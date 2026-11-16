"""Install a daily host backup task, or write a Linux systemd timer template.

Windows uses the current logged-in identity; no password is saved. Docker must
be running. Linux templates are generated for an administrator to install on
the target server; this command never writes system directories implicitly.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from xml.sax.saxutils import escape
from scripts.configure_server import ROOT


def windows_task(root, python, hour=3):
    from scripts.server_backup import _windows_owner_sid
    boundary = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    if boundary <= datetime.now():
        boundary += timedelta(days=1)
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>Consistent RAG server backup with verified optional remote mirror</Description></RegistrationInfo>
<Triggers><CalendarTrigger><StartBoundary>{boundary.isoformat()}</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
<Principals><Principal id="CurrentUser"><UserId>{_windows_owner_sid()}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT2H</ExecutionTimeLimit><Hidden>true</Hidden></Settings>
<Actions Context="CurrentUser"><Exec><Command>{escape(str(python))}</Command><Arguments>-m scripts.backup_schedule --once</Arguments><WorkingDirectory>{escape(str(root))}</WorkingDirectory></Exec></Actions>
</Task>'''


def install(root=ROOT, *, hour=3, register=True):
    root = Path(root).resolve()
    directory = root / "data" / "backup-schedule"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        name = "RAGEnterpriseBackup-" + hashlib.sha256(str(root).encode()).hexdigest()[:10]
        path = directory / "daily-backup.xml"
        path.write_text(windows_task(root, Path(sys.executable).resolve(), hour), encoding="utf-16")
        if register:
            result = subprocess.run(["schtasks.exe", "/Create", "/TN", name, "/XML", str(path), "/F"],
                                    capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise RuntimeError("Windows backup task registration failed")
        return {"task_name": name, "registered": register, "template": str(path), "requires_logged_in_user": True}
    import shlex
    command = shlex.join([str(Path(sys.executable).resolve()), "-m", "scripts.backup_schedule", "--once"])
    (directory / "rag-backup.service").write_text(
        "[Unit]\nDescription=RAG consistent server backup\nAfter=docker.service\n[Service]\nType=oneshot\nWorkingDirectory=" + str(root) + "\nExecStart=" + command + "\n", encoding="utf-8")
    (directory / "rag-backup.timer").write_text(
        f"[Unit]\nDescription=Daily RAG backup\n[Timer]\nOnCalendar=*-*-* {hour:02d}:00:00\nPersistent=true\n[Install]\nWantedBy=timers.target\n", encoding="utf-8")
    return {"registered": False, "template_directory": str(directory), "requires_systemd_install": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--hour", type=int, choices=range(24), default=3)
    parser.add_argument("--template-only", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(install(args.project_root, hour=args.hour, register=not args.template_only), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
