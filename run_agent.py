from __future__ import annotations

from browser_use.professor_search.service import Phase1Input, ProfessorRecruitingAgent, run_from_cli


def create_app():
	try:
		from fastapi import FastAPI
	except ImportError as exc:
		raise RuntimeError('FastAPI is not installed. Install dev dependencies to use API mode.') from exc

	app = FastAPI(title='Professor Recruiting Agent (Phase 1)')
	agent = ProfessorRecruitingAgent()

	@app.post('/match')
	def match(request: Phase1Input):
		try:
			return agent.run_with_metadata(request).model_dump()
		except ValueError as exc:
			from fastapi import HTTPException

			raise HTTPException(status_code=422, detail=str(exc)) from exc

	return app


if __name__ == '__main__':
	raise SystemExit(run_from_cli())
