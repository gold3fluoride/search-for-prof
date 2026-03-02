from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'browser_use' / 'professor_search' / 'service.py'
SPEC = importlib.util.spec_from_file_location('professor_search_service', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

Phase1Input = MODULE.Phase1Input
ProfessorRecruitingAgent = MODULE.ProfessorRecruitingAgent
ProfessorSource = MODULE.ProfessorSource


def test_regex_extraction_open_and_closed(tmp_path):
	agent = ProfessorRecruitingAgent(db_path=tmp_path / 'phase1.db')
	open_status, open_conf, open_evidence = agent.extract_recruiting_evidence(
		'We are accepting new students for Fall 2026. Prospective students welcome.',
		degree_level='phd',
	)
	closed_status, closed_conf, closed_evidence = agent.extract_recruiting_evidence(
		'I am not taking students this cycle due to funding constraints.',
		degree_level='phd',
	)

	assert open_status == 'open'
	assert open_conf >= 0.9
	assert 'accepting new students' in open_evidence.lower()
	assert closed_status == 'closed'
	assert closed_conf >= 0.9
	assert 'not taking students' in closed_evidence.lower()


def test_fit_score_penalizes_closed(tmp_path):
	agent = ProfessorRecruitingAgent(db_path=tmp_path / 'phase1.db')
	open_fit = agent._compute_fit(['nlp', 'systems'], ['nlp'], 'open', 0.9)
	closed_fit = agent._compute_fit(['nlp', 'systems'], ['nlp'], 'closed', 0.95)

	assert open_fit > closed_fit


def test_run_returns_ranked_results_with_schema(tmp_path):
	agent = ProfessorRecruitingAgent(db_path=tmp_path / 'phase1.db')

	agent.discover_institution_pages = lambda institution_name: ['https://example.edu/faculty']
	agent._fetch_html = lambda url: '<html><body>mock</body></html>'
	agent._parse_faculty_links = lambda institution, page_url, html: [
		ProfessorSource(name='Alice Smith', institution=institution, profile_url='https://example.edu/alice'),
		ProfessorSource(name='Bob Lee', institution=institution, profile_url='https://example.edu/bob'),
	]
	agent._resolve_professor_urls = lambda source: source

	def fake_crawl(source, degree_level):
		if source.name == 'Alice Smith':
			return ('open', 0.92, 'Accepting new students', source.profile_url, ['nlp'], [(source.profile_url, 'Alice')])
		return ('unclear', 0.4, 'No explicit recruiting statement found.', source.profile_url, ['robotics'], [(source.profile_url, 'Bob')])

	agent._crawl_professor = fake_crawl

	results = agent.run(
		Phase1Input(
			interests=['nlp'],
			target_institutions=['Example University'],
			degree_level='phd',
		)
	)

	assert len(results) == 2
	assert results[0].name == 'Alice Smith'
	assert results[0].fit_score >= results[1].fit_score
	assert results[0].recruiting_status == 'open'
	assert results[0].evidence_url.startswith('https://')
