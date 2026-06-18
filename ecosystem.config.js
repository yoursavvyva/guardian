module.exports = {
  apps: [{
    name: "guardian-assistant",
    script: "app/main.py",
    interpreter: "/usr/bin/python3",
    cwd: "/home/darcee/projects/guardian",
    env: { PYTHONUNBUFFERED: "1", PYTHONPATH: "/home/darcee/projects/guardian" },
    autorestart: true,
    max_restarts: 10,
    out_file: "logs/guardian.out.log",
    error_file: "logs/guardian.err.log",
    time: true
  }]
};
