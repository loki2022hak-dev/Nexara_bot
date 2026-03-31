
import asyncio
import subprocess

async def run_cmd(cmd):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode(), stderr.decode()
    except Exception as e:
        return "", str(e)

async def run_maigret(target):
    stdout, stderr = await run_cmd([
        "/opt/maigret-venv/bin/python",
        "-m", "maigret",
        target,
        "--timeout", "15",
        "--top-sites", "50",
        "--no-color"
    ])
    return stdout

async def run_sherlock(target):
    stdout, stderr = await run_cmd([
        "sherlock",
        target,
        "--timeout", "10"
    ])
    return stdout

def parse_output(text):
    results = []
    for line in text.splitlines():
        if "http" in line:
            results.append(line.strip())
    return results

async def full_search(target):
    maigret_out = await run_maigret(target)
    sherlock_out = await run_sherlock(target)

    parsed = parse_output(maigret_out + "\n" + sherlock_out)

    if not parsed:
        return "❌ НІЧОГО НЕ ЗНАЙДЕНО"

    msg = "🔎 РЕЗУЛЬТАТИ:\n\n"
    for i, r in enumerate(parsed[:20], 1):
        msg += f"{i}. {r}\n"

    return msg
