from __future__ import annotations

from browser_use.professor_search.models import RecruitingEvidence, RecruitingStatus
from browser_use.professor_search.service import Phase1Input, ProfessorRecruitingAgent, ProfessorSource


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

	agent.discover_faculty_pages = lambda institution_name: ['https://example.edu/faculty']
	agent._fetch_html = lambda url, reason='crawl': '<html><body>mock</body></html>'
	agent._parse_faculty_links = lambda institution, page_url, html: [
		ProfessorSource(name='Alice Smith', institution=institution, profile_url='https://example.edu/alice'),
		ProfessorSource(name='Bob Lee', institution=institution, profile_url='https://example.edu/bob'),
	]
	agent._resolve_professor_urls = lambda source: source

	def fake_crawl(seed_urls, limits, allowed_domains, degree_level):
		if seed_urls[0].endswith('/alice'):
			return (
				RecruitingEvidence(
					status=RecruitingStatus.OPEN,
					confidence=0.92,
					evidence_text='Accepting new students',
					evidence_url=seed_urls[0],
					checked_at='2026-01-01T00:00:00+00:00',
				),
				['nlp'],
				[(seed_urls[0], 'Alice', 'seed')],
			)
		return (
			RecruitingEvidence(
				status=RecruitingStatus.UNCLEAR,
				confidence=0.4,
				evidence_text='No explicit recruiting statement found.',
				evidence_url=seed_urls[0],
				checked_at='2026-01-01T00:00:00+00:00',
			),
			['robotics'],
			[(seed_urls[0], 'Bob', 'seed')],
		)

	agent.crawl_professor_pages = fake_crawl

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
	assert results[0].recruiting_status.value == 'open'
	assert results[0].evidence_url.startswith('https://')


def test_run_with_metadata_includes_run_id_and_input_echo(tmp_path):
	agent = ProfessorRecruitingAgent(db_path=tmp_path / 'phase1.db')
	agent.discover_faculty_pages = lambda institution_name: []

	request = Phase1Input(interests=['nlp'], target_institutions=['Example University'], degree_level='phd')
	payload = agent.run_with_metadata(request)

	assert payload.run_id > 0
	assert payload.input_echo.target_institutions == ['Example University']
	assert payload.results == []
