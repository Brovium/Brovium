# src/utils.py

import os
import shutil
import subprocess

def run_command(command, title=""):
    """
    执行外部命令，并传递当前环境变量。
    """
    try:
        # 调用前打印可执行文件路径（第一项一般是 duarouter 或 netconvert）
        exe = command[0] if isinstance(command, (list, tuple)) else str(command).split()[0]
        resolved = shutil.which(exe)

        print(f"\n--- 步骤: {title} ---")
        if resolved:
            print(f"→ 执行: {exe} ({resolved})")
        else:
            print(f"→ 执行: {exe} (未在 PATH 中解析)")

        # 把当前环境（含 SUMO_HOME / PATH / PROJ_LIB）传给子进程
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=600,
            env=os.environ
        )
        if result.stdout:
            print(result.stdout.decode("utf-8", errors="ignore").strip())
        print(f"✓ {title} 成功。")
        return True
    except subprocess.CalledProcessError as e:
        if e.stdout:
            print(e.stdout.decode("utf-8", errors="ignore"))
        if e.stderr:
            print(e.stderr.decode("utf-8", errors="ignore"))
        print(f"✗ {title} 失败。")
        return False
    except Exception as e:
        print(f"✗ {title} 异常: {e}")
        return False