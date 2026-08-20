#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { spawn, spawnSync } from "node:child_process";

let cachedBrowser = null;


function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, ""));
}


function writeJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  fs.renameSync(temporary, filePath);
}


function loadPlaywright(nodeModules) {
  const candidates = [
    nodeModules,
    process.env.VIDEO_OS_NODE_MODULES,
    ...(process.env.NODE_PATH || "").split(path.delimiter),
  ].filter(Boolean);
  for (const root of candidates) {
    if (!fs.existsSync(path.join(root, "playwright", "package.json"))) continue;
    const requireFromRoot = createRequire(path.join(root, "worker-loader.cjs"));
    return requireFromRoot("playwright");
  }
  throw new Error(
    `Playwright was not found. Checked: ${candidates.join(", ")}`,
  );
}


function parseArgs(argv) {
  const [command = "doctor", ...rest] = argv;
  const result = { command };
  for (let index = 0; index < rest.length; index += 1) {
    const token = rest[index];
    if (!token.startsWith("--")) throw new Error(`Unexpected argument: ${token}`);
    const key = token.slice(2).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
    const next = rest[index + 1];
    if (next == null || next.startsWith("--")) result[key] = true;
    else {
      result[key] = next;
      index += 1;
    }
  }
  return result;
}


function log(config, level, event, data = {}) {
  const record = {
    at: new Date().toISOString(),
    level,
    event,
    ...data,
  };
  if (config.logPath) {
    fs.mkdirSync(path.dirname(config.logPath), { recursive: true });
    fs.appendFileSync(config.logPath, `${JSON.stringify(record)}\n`, "utf8");
  }
  process.stdout.write(`${JSON.stringify(record)}\n`);
}


async function endpointReady(port) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/json/version`);
    return response.ok;
  } catch {
    return false;
  }
}


async function ensureBrowser(config) {
  if (await endpointReady(config.remoteDebuggingPort)) return false;
  fs.mkdirSync(config.userDataDir, { recursive: true });
  const browserPath = config.browserPath || config.chromePath;
  if (!browserPath) {
    throw new Error("Worker config has no browserPath (legacy chromePath is also accepted)");
  }
  const browserArgs = [
    `--remote-debugging-port=${config.remoteDebuggingPort}`,
    `--user-data-dir=${config.userDataDir}`,
    "--profile-directory=Default",
    `--app=${config.geminiUrl}`,
    "--no-first-run",
    "--no-default-browser-check",
  ];
  const child = spawn(browserPath, browserArgs, {
    detached: true,
    stdio: "ignore",
    windowsHide: false,
  });
  child.unref();
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    if (await endpointReady(config.remoteDebuggingPort)) return true;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Browser debugging port ${config.remoteDebuggingPort} did not open`);
}


async function connectPage(config, chromium) {
  await ensureBrowser(config);
  if (!cachedBrowser || !cachedBrowser.isConnected()) {
    cachedBrowser = await chromium.connectOverCDP(
      `http://127.0.0.1:${config.remoteDebuggingPort}`,
    );
  }
  const browser = cachedBrowser;
  const context = browser.contexts()[0];
  if (!context) throw new Error("Browser exposed no context over CDP");
  let page = context.pages().find((candidate) => candidate.url().includes("gemini.google.com"));
  if (!page) {
    page = context
      .pages()
      .find((candidate) => /accounts\.google\.com|gds\.google\.com/.test(candidate.url()));
  }
  if (!page) {
    page = await context.newPage();
    await page.goto(config.geminiUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  }
  await page.bringToFront();
  return { browser, context, page };
}


async function startFreshConversation(page, config) {
  await page.goto(config.geminiUrl, {
    waitUntil: "domcontentloaded",
    timeout: 60000,
  });
  await page.waitForTimeout(500);
}


async function pageReadiness(page) {
  const url = page.url();
  const title = await page.title();
  const bodyText = (await page.locator("body").innerText({ timeout: 15000 })).slice(0, 6000);
  const loginPattern = /Sign in|登录|登入|使用 Google 账号|Choose an account|Use your Google Account/i;
  const editors = await visibleCandidates(page, [
    'rich-textarea div[contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
    'textarea',
  ]);
  return {
    url,
    title,
    status: editors.length
      ? "ready"
      : /accounts\.google\.com|gds\.google\.com/.test(url) || loginPattern.test(bodyText)
        ? "needs_login"
        : "needs_human",
    editorCount: editors.length,
    fileInputCount: await page.locator('input[type="file"]').count(),
    bodyPreview: bodyText.slice(0, 1200),
  };
}


async function visibleCandidates(page, selectors) {
  for (const selector of selectors) {
    const matches = [];
    const locator = page.locator(selector);
    const count = await locator.count();
    for (let index = 0; index < count; index += 1) {
      const candidate = locator.nth(index);
      if (await candidate.isVisible().catch(() => false)) matches.push(candidate);
    }
    if (matches.length) return matches;
  }
  return [];
}


function runPrepare(config, args, { allowFailure = false } = {}) {
  const result = spawnSync(
    config.pythonPath,
    [config.prepareScript, ...args],
    { encoding: "utf8", windowsHide: true },
  );
  if (result.status !== 0 && !allowFailure) {
    throw new Error(
      `prepare_perception failed (${args.join(" ")}): ${(result.stderr || result.stdout).trim()}`,
    );
  }
  return result;
}


function queuedTasks(projectDir, kind = "perception") {
  const queueDir = path.join(
    projectDir,
    kind === "review" ? "review" : "perception",
    "tasks",
    "queued",
  );
  if (!fs.existsSync(queueDir)) return [];
  return fs
    .readdirSync(queueDir)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .map((name) => readJson(path.join(queueDir, name)));
}


function configuredProjects(config, requestedProject) {
  if (requestedProject) return [path.resolve(requestedProject)];
  const projects = [];
  for (const configured of config.projectRoots || []) {
    const root = path.resolve(configured);
    if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) continue;
    if (
      fs.existsSync(path.join(root, "perception", "tasks")) ||
      fs.existsSync(path.join(root, "review", "tasks"))
    ) projects.push(root);
    for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const candidate = path.join(root, entry.name);
      if (
        fs.existsSync(path.join(candidate, "perception", "tasks")) ||
        fs.existsSync(path.join(candidate, "review", "tasks"))
      ) {
        projects.push(candidate);
      }
    }
  }
  return [...new Set(projects)];
}


function discoverTask(config, requestedProject, requestedKind, requestedTaskId) {
  const projects = configuredProjects(config, requestedProject);
  for (const projectDir of projects) {
    if (!requestedKind || requestedKind === "perception") {
      const perceptionTasks = queuedTasks(projectDir, "perception").filter(
        (task) => !requestedTaskId || task.task_id === requestedTaskId,
      );
      if (perceptionTasks.length) {
        return { projectDir, task: perceptionTasks[0], kind: "perception" };
      }
    }
    if (!requestedKind || requestedKind === "review") {
      const reviewTasks = queuedTasks(projectDir, "review").filter(
        (task) => !requestedTaskId || task.task_id === requestedTaskId,
      );
      if (reviewTasks.length) {
        return { projectDir, task: reviewTasks[0], kind: "review" };
      }
    }
  }
  return null;
}


function processIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}


function acquireRunLock(config) {
  const lockPath = path.resolve(
    config.lockPath || path.join(path.dirname(config.logPath), "worker.lock"),
  );
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });
  if (fs.existsSync(lockPath)) {
    let existingPid = 0;
    try {
      existingPid = Number(readJson(lockPath).pid || 0);
    } catch {
      existingPid = 0;
    }
    if (processIsAlive(existingPid)) {
      throw new Error(`Gemini Worker is already running with PID ${existingPid}`);
    }
    fs.unlinkSync(lockPath);
  }
  const descriptor = fs.openSync(lockPath, "wx");
  fs.writeFileSync(
    descriptor,
    `${JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }, null, 2)}\n`,
    "utf8",
  );
  fs.closeSync(descriptor);
  process.on("exit", () => {
    try {
      const current = readJson(lockPath);
      if (Number(current.pid) === process.pid) fs.unlinkSync(lockPath);
    } catch {
      // A stale or replaced lock is handled on the next start.
    }
  });
  return lockPath;
}


function taskTransition(config, projectDir, taskId, state, error = null, kind = "perception") {
  const args = [
    kind === "review" ? "review-transition" : "transition",
    projectDir,
    taskId,
    state,
    "--worker-id",
    config.workerId,
  ];
  if (error) args.push("--error", error.slice(0, 1000));
  runPrepare(config, args);
}


function buildPrompt(config, task) {
  const contractPath = path.resolve(config.skillRoot, task.prompt_contract);
  const contract = fs.readFileSync(contractPath, "utf8");
  let script = "";
  if (task.script_path && fs.existsSync(task.script_path)) {
    script = fs.readFileSync(task.script_path, "utf8").slice(0, 20000);
  }
  const parts = [
    contract,
    "",
    `Task type: ${task.task_type || "perception"}`,
    `Task id: ${task.task_id}`,
  ];
  if (task.task_type === "review") {
    parts.push(
      "",
      "Target metadata:",
      JSON.stringify(
        {
          path: task.target,
          duration: task.target_duration,
          signature: task.target_signature,
        },
        null,
        2,
      ),
    );
    if (task.edit_plan_path && fs.existsSync(task.edit_plan_path)) {
      try {
        const plan = JSON.parse(fs.readFileSync(task.edit_plan_path, "utf8"));
        parts.push(
          "",
          "Edit plan context (timeline only, for alignment expectations):",
          JSON.stringify(
            {
              duration_seconds: plan.duration_seconds,
              semantic_sections: (plan.semantic_sections || []).map((section) => ({
                id: section.id,
                start: section.start,
                end: section.end,
                intent: section.intent,
              })),
              fullscreen_events: (plan.fullscreen_events || []).map((event) => ({
                id: event.id,
                timeline_start: event.timeline_start,
                duration: event.duration,
                intent: event.intent,
                source_start: event.source_start,
              })),
              sound_effects: (plan.sound_effects || []),
            },
            null,
            2,
          ).slice(0, 16000),
        );
      } catch {
        // Edit plan context is optional; continue without it.
      }
    }
  } else {
    parts.push(
      "",
      "Task metadata (copy these values exactly into the response):",
      JSON.stringify(
        {
          source: task.source,
          duration: task.source_duration,
          signature: task.source_signature,
        },
        null,
        2,
      ),
    );
  }
  if (script) parts.push(`\nClient script for alignment context only:\n${script}`);
  parts.push("", "Return JSON only. Analyze the complete uploaded video before answering.");
  return parts.join("\n");
}


async function clickFileChooser(page, control, proxyPath, timeout = 5000) {
  const enabled = await control.isEnabled().catch(() => false);
  if (!enabled) return false;
  const chooserPromise = page
    .waitForEvent("filechooser", { timeout })
    .catch(() => null);
  await control.click();
  const chooser = await chooserPromise;
  if (!chooser) return false;
  await chooser.setFiles(proxyPath);
  return true;
}


function withStageTimeout(promise, seconds, label) {
  let timer = null;
  const timeout = new Promise((_resolve, reject) => {
    timer = setTimeout(
      () => reject(new Error(`stage timeout after ${seconds}s during ${label}`)),
      seconds * 1000,
    );
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}


async function uploadProxy(page, task, config) {
  const proxyPath = path.resolve(task.proxy_path);
  if (!fs.existsSync(proxyPath)) throw new Error(`Proxy file is missing: ${proxyPath}`);

  async function tryFileChooser(control, timeout = 8000) {
    if (!(await control.isVisible().catch(() => false))) return false;
    if (!(await control.isEnabled().catch(() => false))) return false;
    return clickFileChooser(page, control, proxyPath, timeout);
  }

  // Close any stale menu from a previous attempt before starting.
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForTimeout(400);

  let fileChooserUsed = false;
  let inputs = page.locator('input[type="file"]');
  if ((await inputs.count().catch(() => 0)) > 0) {
    fileChooserUsed = await tryFileChooser(inputs.first(), 2000);
  }

  // Primary upload button opens a menu or directly a file chooser.
  if (!fileChooserUsed) {
    const primaryPatterns = config.uploadButtonNames || [
      "上传和工具",
      "Upload and tools",
      "上传",
      "Upload",
    ];
    for (const name of primaryPatterns) {
      const buttons = page.getByRole("button", { name, exact: false });
      const count = await buttons.count().catch(() => 0);
      for (let index = 0; index < count; index += 1) {
        fileChooserUsed = await tryFileChooser(buttons.nth(index), 2000);
        if (fileChooserUsed) break;
      }
      if (fileChooserUsed) break;
    }
  }

  // Menu is open now: click the 上传文件 item.
  if (!fileChooserUsed) {
    await page.waitForTimeout(500);
    const menuPatterns = config.uploadMenuNames || [
      "上传文件",
      "Upload files",
      "从设备上传",
      "Upload from device",
    ];
    for (const name of menuPatterns) {
      const candidates = page.locator(
        `[role="menuitem"]:has-text("${name}"), button:has-text("${name}")`,
      );
      const count = await candidates.count().catch(() => 0);
      for (let index = 0; index < count; index += 1) {
        fileChooserUsed = await tryFileChooser(candidates.nth(index), 8000);
        if (fileChooserUsed) break;
      }
      if (fileChooserUsed) break;
    }
  }

  // Last fallback: reopen the menu and try the file item once more.
  if (!fileChooserUsed) {
    const openButton = page.getByRole("button", {
      name: /上传和工具|Upload and tools/i,
    });
    const openCount = await openButton.count().catch(() => 0);
    for (let index = 0; index < openCount; index += 1) {
      const candidate = openButton.nth(index);
      if (!(await candidate.isVisible().catch(() => false))) continue;
      if (!(await candidate.isEnabled().catch(() => false))) continue;
      await candidate.click().catch(() => {});
      break;
    }
    await page.waitForTimeout(600);
    const fallback = page.locator(
      '[role="menuitem"]:has-text("上传文件"), button:has-text("上传文件")',
    );
    const fallbackCount = await fallback.count().catch(() => 0);
    for (let index = 0; index < fallbackCount; index += 1) {
      fileChooserUsed = await tryFileChooser(fallback.nth(index), 8000);
      if (fileChooserUsed) break;
    }
  }

  if (!fileChooserUsed) {
    inputs = page.locator('input[type="file"]');
    const inputCount = await inputs.count().catch(() => 0);
    if (inputCount > 0) {
      let selected = null;
      for (let index = 0; index < inputCount; index += 1) {
        const candidate = inputs.nth(index);
        const accept = (await candidate.getAttribute("accept").catch(() => "")) || "";
        if (!selected || /video|mp4|quicktime|\*/i.test(accept)) selected = candidate;
        if (/video|mp4|quicktime/i.test(accept)) break;
      }
      if (selected) {
        await selected.setInputFiles(proxyPath);
        fileChooserUsed = true;
      }
    }
  }
  if (!fileChooserUsed) {
    throw new Error("Gemini file upload control did not open a file chooser");
  }

  const filename = path.basename(proxyPath);
  const attachmentText = page.getByText(filename, { exact: false }).last();
  const attachmentControl = page.locator([
    ".gem-attachment-content",
    'button[aria-label="关闭附件"]',
    'button[aria-label*="上传的视频"]',
    'button[aria-label*="上传的文件"]',
    'button[aria-label*="uploaded video" i]',
    'button[aria-label*="uploaded file" i]',
  ].join(", ")).first();
  const uploadDeadline = Date.now() + 90000;
  let appeared = false;
  while (Date.now() < uploadDeadline) {
    const textVisible = await attachmentText.isVisible().catch(() => false);
    const controlVisible = await attachmentControl.isVisible().catch(() => false);
    const sendVisible = await page
      .getByRole("button", { name: /发送|Send/i })
      .first()
      .isVisible()
      .catch(() => false);
    if (textVisible || controlVisible || sendVisible) {
      appeared = true;
      break;
    }
    await page.waitForTimeout(1000);
  }
  // Gemini may show an upload-progress indicator instead of the filename.
  // The send button becoming enabled is the definitive readiness signal;
  // submitPrompt waits for it, so an upload that is still indexing can
  // proceed rather than being abandoned here.
  return filename;
}


async function submitPrompt(page, prompt, config) {
  const editors = await visibleCandidates(page, config.editorSelectors || [
    'rich-textarea div[contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
    'textarea',
  ]);
  if (editors.length !== 1) {
    throw new Error(`Expected one visible Gemini prompt editor; found ${editors.length}`);
  }
  await editors[0].fill(prompt);
  const sendSelectors = config.sendSelectors || [
    'button[data-test-id="send-button"]',
    'button[aria-label*="Send message"]',
    'button[aria-label*="Send"]',
    'button[aria-label*="发送"]',
    "button.send-button",
  ];
  const sendButtons = await visibleCandidates(page, sendSelectors);
  if (sendButtons.length !== 1) {
    throw new Error(`Expected one visible Gemini send button; found ${sendButtons.length}`);
  }
  const sendButton = sendButtons[0];
  const sendDeadline =
    Date.now() + Number(config.stageTimeoutSeconds || 240) * 1000;
  let enabled = false;
  while (Date.now() < sendDeadline) {
    enabled = await sendButton.isEnabled().catch(() => false);
    if (enabled) break;
    await page.waitForTimeout(2000);
  }
  if (!enabled) {
    throw new Error(
      "Gemini send button stayed disabled; the video is still processing",
    );
  }
  await sendButton.click();
}


async function responseTexts(page, selectors) {
  for (const selector of selectors) {
    const locator = page.locator(selector);
    const count = await locator.count();
    if (!count) continue;
    const values = [];
    for (let index = 0; index < count; index += 1) {
      const value = (await locator.nth(index).innerText().catch(() => "")).trim();
      if (value) values.push(value);
    }
    if (values.length) return values;
  }
  return [];
}


async function waitForResponse(page, config, initialCount) {
  const selectors = config.responseSelectors || [
    "message-content .markdown-main-panel",
    "message-content.model-response-text",
    "message-content",
    "model-response .response-container-content",
    ".model-response-text",
    '[data-test-id="model-response"]',
  ];
  const deadline =
    Date.now() + Number(config.stageTimeoutSeconds || 240) * 1000;
  let prior = "";
  let stable = 0;
  while (Date.now() < deadline) {
    const consentButtons = await visibleCandidates(page, [
      'button:has-text("同意")',
      'button:has-text("I agree")',
      'button:has-text("Agree")',
    ]);
    if (consentButtons.length) {
      throw new Error(
        "Gemini policy consent requires manual acceptance in the visible Worker window",
      );
    }
    const values = await responseTexts(page, selectors);
    const current = values.length > initialCount ? values.at(-1) : "";
    if (current && current === prior) stable += 1;
    else stable = 0;
    if (current && stable >= 3 && current.includes("{")) return current;
    prior = current;
    await page.waitForTimeout(2000);
  }
  throw new Error("Gemini response timed out or never stabilized");
}


export function extractJson(text) {
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = (fenced ? fenced[1] : text).trim();
  try {
    return JSON.parse(candidate);
  } catch {
    const start = candidate.indexOf("{");
    const end = candidate.lastIndexOf("}");
    if (start < 0 || end <= start) throw new Error("Gemini response contains no JSON object");
    return JSON.parse(candidate.slice(start, end + 1));
  }
}


async function processOne(
  config,
  chromium,
  requestedProject,
  requestedKind,
  requestedTaskId,
  failClosed = false,
) {
  const discovered = discoverTask(
    config,
    requestedProject,
    requestedKind,
    requestedTaskId,
  );
  if (!discovered) return { ok: true, status: "idle" };
  const { projectDir, task, kind } = discovered;
  const taskId = task.task_id;
  const stageSeconds = Number(config.stageTimeoutSeconds || 240);
  taskTransition(config, projectDir, taskId, "running", null, kind);
  let currentState = "running";
  try {
    return await withStageTimeout(runTaskStage(config, chromium, projectDir, task, kind), stageSeconds, kind);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (!["done", "needs_login", "needs_human", "failed"].includes(currentState)) {
      const attempts = Number(task.attempts || 0);
      const maxRetries = Number(config.maxRetries || 3);
      const retryable = ["running", "uploading", "uploaded", "analyzing", "validating"].includes(currentState);
      const destination = failClosed
        ? "needs_human"
        : retryable && attempts < maxRetries
          ? "queued"
        : /login/i.test(message)
          ? "needs_login"
          : /not found|Expected one|selector|editor|control|consent|manual|human/i.test(message)
            ? "needs_human"
            : "failed";
      runPrepare(
        config,
        [
          kind === "review" ? "review-transition" : "transition",
          projectDir,
          taskId,
          destination,
          "--worker-id",
          config.workerId,
          "--error",
          message.slice(0, 1000),
        ],
        { allowFailure: true },
      );
    }
    throw error;
  }
}


async function runTaskStage(config, chromium, projectDir, task, kind) {
  const { browser, page } = await connectPage(config, chromium);
  const taskId = task.task_id;
  await startFreshConversation(page, config);
  const readiness = await pageReadiness(page);
  if (readiness.status === "needs_login") {
    taskTransition(config, projectDir, taskId, "needs_login", "Gemini login is required", kind);
    return { ok: false, status: "needs_login", readiness };
  }
  if (readiness.status !== "ready") {
    taskTransition(config, projectDir, taskId, "needs_human", "Gemini prompt editor was not detected", kind);
    return { ok: false, status: "needs_human", readiness };
  }
  taskTransition(config, projectDir, taskId, "uploading", null, kind);
  await uploadProxy(page, task, config);
  taskTransition(config, projectDir, taskId, "uploaded", null, kind);
  const initialResponses = await responseTexts(
    page,
    config.responseSelectors || ["message-content.model-response-text", ".model-response-text"],
  );
  const prompt = buildPrompt(config, task);
  await submitPrompt(page, prompt, config);
  taskTransition(config, projectDir, taskId, "analyzing", null, kind);
  const responseText = await waitForResponse(page, config, initialResponses.length);
  const responseJson = extractJson(responseText);
  const responseDir =
    kind === "review"
      ? path.join(projectDir, "review", "provider_responses")
      : path.join(projectDir, "perception", "provider_responses");
  fs.mkdirSync(responseDir, { recursive: true });
  const responsePath = path.join(responseDir, `${taskId}.json`);
  writeJson(responsePath, responseJson);
  taskTransition(config, projectDir, taskId, "validating", null, kind);
  if (kind === "review") {
    runPrepare(config, [
      "import-review-result",
      projectDir,
      taskId,
      responsePath,
      "--worker-id",
      config.workerId,
    ]);
  } else {
    runPrepare(config, [
      "import-result",
      projectDir,
      taskId,
      responsePath,
      "--worker-id",
      config.workerId,
    ]);
  }
  const remaining = queuedTasks(projectDir, kind);
  if (kind === "perception" && !remaining.length) {
    const mergeArgs = ["merge", projectDir];
    if (config.ffprobePath) mergeArgs.push("--ffprobe", config.ffprobePath);
    runPrepare(config, mergeArgs);
  }
  return { ok: true, status: "done", projectDir, taskId, kind, responsePath };
}


async function doctor(config, chromium) {
  const launched = await ensureBrowser(config);
  const { page } = await connectPage(config, chromium);
  const readiness = await pageReadiness(page);
  const buttonPreview = await page.locator("button:visible").evaluateAll((buttons) =>
    buttons.slice(0, 80).map((button) => ({
      ariaLabel: button.getAttribute("aria-label") || "",
      title: button.getAttribute("title") || "",
      text: (button.innerText || "").trim().slice(0, 120),
    })).filter((item) => item.ariaLabel || item.title || item.text),
  );
  return {
    ok: readiness.status === "ready",
    launched,
    endpoint: `http://127.0.0.1:${config.remoteDebuggingPort}`,
    ...readiness,
    buttonPreview,
  };
}


async function browserStatus(config, chromium) {
  if (!(await endpointReady(config.remoteDebuggingPort))) {
    return {
      ok: true,
      status: "stopped",
      endpoint: `http://127.0.0.1:${config.remoteDebuggingPort}`,
    };
  }
  const browser = await chromium.connectOverCDP(
    `http://127.0.0.1:${config.remoteDebuggingPort}`,
  );
  const context = browser.contexts()[0];
  if (!context) {
    return { ok: false, status: "needs_human", reason: "Browser exposed no context over CDP" };
  }
  const page = context.pages().find((candidate) =>
    /gemini\.google\.com|accounts\.google\.com|gds\.google\.com/.test(candidate.url()),
  );
  if (!page) {
    return { ok: false, status: "needs_human", reason: "Gemini page was not found" };
  }
  const readiness = await pageReadiness(page);
  return {
    ok: readiness.status === "ready",
    status: readiness.status,
    url: readiness.url,
    title: readiness.title,
    editorCount: readiness.editorCount,
    fileInputCount: readiness.fileInputCount,
  };
}


async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.command === "self-test") {
    const parsed = extractJson('prefix```json\n{"ok":true,"sources":[]}\n```suffix');
    if (parsed.ok !== true) throw new Error("JSON extraction self-test failed");
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "video-os-worker-test-"));
    try {
      for (const kind of ["perception", "review"]) {
        const queued = path.join(temporary, kind, "tasks", "queued");
        fs.mkdirSync(queued, { recursive: true });
        writeJson(path.join(queued, `${kind}.json`), { task_id: kind });
      }
      const selected = discoverTask({}, temporary, "review", "review");
      if (!selected || selected.kind !== "review" || selected.task.task_id !== "review") {
        throw new Error("Review-only discovery self-test failed");
      }
    } finally {
      fs.rmSync(temporary, { recursive: true, force: true });
    }
    process.stdout.write(`${JSON.stringify({ ok: true, command: "self-test" }, null, 2)}\n`);
    return;
  }
  if (!args.config) throw new Error("--config is required");
  const config = readJson(path.resolve(args.config));
  if (args.kind && !["perception", "review"].includes(args.kind)) {
    throw new Error(`Unknown task kind: ${args.kind}`);
  }
  if (args.prepareScript) config.prepareScript = path.resolve(args.prepareScript);
  if (args.skillRoot) config.skillRoot = path.resolve(args.skillRoot);
  const { chromium } = loadPlaywright(config.nodeModules);
  if (args.command === "status") {
    const result = await browserStatus(config, chromium);
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    process.exit(0);
  }
  if (args.command === "doctor") {
    const result = await doctor(config, chromium);
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    process.exit(result.ok ? 0 : 2);
  }
  if (args.command === "once") {
    const result = await processOne(
      config,
      chromium,
      args.project,
      args.kind,
      args.taskId,
      Boolean(args.failClosed),
    );
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    process.exit(result.ok ? 0 : 2);
  }
  if (args.command === "run") {
    const lockPath = acquireRunLock(config);
    log(config, "info", "worker_started", { pid: process.pid, lockPath });
    let lastStatus = "";
    let lastHeartbeatAt = 0;
    while (true) {
      try {
        const result = await processOne(
          config,
          chromium,
          args.project,
          args.kind,
          args.taskId,
        );
        const now = Date.now();
        if (result.status !== lastStatus || now - lastHeartbeatAt >= 300000) {
          log(config, "info", "worker_cycle", result);
          lastStatus = result.status;
          lastHeartbeatAt = now;
        }
      } catch (error) {
        log(config, "error", "worker_cycle_failed", {
          error: error instanceof Error ? error.message : String(error),
        });
      }
      await new Promise((resolve) =>
        setTimeout(resolve, Number(config.pollSeconds || 10) * 1000),
      );
    }
  }
  throw new Error(`Unknown command: ${args.command}`);
}


main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exit(1);
});
