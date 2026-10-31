"""A real startup contract, shared by old baseline and candidate (no new imports)."""
import asyncio
import os

os.environ['SKILLS_ENABLED'] = 'false'
os.environ['SKILL_WRITE_PLUGIN'] = 'synthetic_nonexistent_admin_module:plugin'

from app.bootstrap import build_runtime, dispose_runtime


async def main():
    try:
        runtime = await build_runtime()
    except ValueError as error:
        assert 'SKILL_WRITE_PLUGIN' in str(error), str(error)
        print('ADMIN_REGISTRATION_VALIDATED_WHILE_SKILLS_DISABLED', flush=True)
        return
    await dispose_runtime(runtime)
    print('MISSING_FEATURE_ADMIN_REGISTRATION_SILENTLY_IGNORED', flush=True)
    raise SystemExit(42)


asyncio.run(main())
