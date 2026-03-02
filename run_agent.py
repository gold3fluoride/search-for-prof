from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent / 'browser_use' / 'professor_search' / 'service.py'
SPEC = importlib.util.spec_from_file_location('professor_search_service', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

Phase1Input = MODULE.Phase1Input
ProfessorRecruitingAgent = MODULE.ProfessorRecruitingAgent
run_from_cli = MODULE.run_from_cli


def create_app():
	try:
		from fastapi import FastAPI
	except Exception as exc:
		raise RuntimeError('FastAPI is not installed. Install dev dependencies to use API mode.') from exc

	app = FastAPI(title='Professor Recruiting Agent (Phase 1)')
	agent = ProfessorRecruitingAgent()

	@app.post('/match')
	def match(request: Phase1Input):
		return [x.model_dump() for x in agent.run(request)]

	return app


if __name__ == '__main__':
	raise SystemExit(run_from_cli())
