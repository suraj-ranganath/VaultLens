const { spawnSync } = require("child_process");

exports.handler = async function handler(event) {
  const env = {
    ...process.env,
    PYTHONPATH: [
      process.env.LAMBDA_TASK_ROOT || "/var/task",
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
  const child = spawnSync("python3", ["cloud/lambda_bridge.py"], {
    cwd: process.env.LAMBDA_TASK_ROOT || "/var/task",
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
