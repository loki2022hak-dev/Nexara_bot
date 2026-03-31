import asyncio

async def run_maigret(username: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "maigret",
            username,
            "--top-sites", "50",
            "--no-color",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await proc.communicate()
        output = stdout.decode(errors="ignore")

        results = []
        for line in output.splitlines():
            if "http" in line:
                results.append(line.strip())

        if not results:
            return "❌ Нічого не знайдено"

        msg = "🔎 РЕЗУЛЬТАТ:\n\n"
        for i, r in enumerate(results[:20], 1):
            msg += f"{i}. {r}\n"

        return msg

    except Exception as e:
        return f"❌ Помилка: {e}"
