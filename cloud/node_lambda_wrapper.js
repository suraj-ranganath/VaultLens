const { existsSync } = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

exports.handler = async function handler(event) {
  const taskRoot = process.env.LAMBDA_TASK_ROOT || "/var/task";
  const venvPython = path.join(taskRoot, ".venv", "bin", "python");
  const python = process.env.VAULT_PYTHON || (existsSync(venvPython) ? venvPython : "python3");
  const env = {
    ...process.env,
    PYTHONPATH: [
      taskRoot,
      process.env.PYTHONPATH || "",
    ]
      .filter(Boolean)
      .join(":"),
    LD_LIBRARY_PATH: [
      "/usr/lib64",
      "/lib64",
      "/usr/lib",
      "/lib",
      process.env.LD_LIBRARY_PATH || "",
    ]
      .filter(Boolean)
      .join(":"),
  };
  const child = spawnSync(python, ["cloud/lambda_bridge.py"], {
    cwd: taskRoot,
    env,
    input: JSON.stringify({ event }),
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
  });

  if (child.error) {
    throw child.error;
  }
  if (child.status !== 0) {
    throw new Error(child.stderr || `Python bridge exited with ${child.status}`);
  }
  return JSON.parse(child.stdout || "{}");
};
