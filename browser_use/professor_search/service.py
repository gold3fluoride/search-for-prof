from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib import robotparser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

RecruitingStatus = Literal['open', 'closed', 'unclear']
ROLE_KEYWORDS = ('intern', 'master', 'masters', 'ms', 'phd', 'postdoc')
RELEVANT_LINK_HINTS = ('prospective', 'join', 'openings', 'positions', 'phd', 'intern')
RESEARCH_KEYWORDS = {
	'nlp',
	'llm',
	'information retrieval',
	'ir',
	'machine learning',
	'computer vision',
	'robotics',
	'distributed systems',
	'systems',
	'security',
	'databases',
	'hci',
	'algorithms',
	'reinforcement learning',
	'alignment',
	'networking',
}


class Phase1Input(BaseModel):
	interests: list[str]
	target_institutions: list[str]
	degree_level: Literal['intern', 'masters', 'phd'] | None = None
	start_term: str | None = None
	notes: str | None = None


class Phase1Result(BaseModel):
	name: str
	institution: str
	lab: str | None = None
	research_areas: list[str] = Field(default_factory=list)
	recruiting_status: RecruitingStatus
	status_confidence: float
	fit_score: float
	evidence_text: str
	evidence_url: str
	checked_at: str


@dataclass
class ProfessorSource:
	name: str
	institution: str
	profile_url: str
	homepage_url: str | None = None
	lab_url: str | None = None


class ProfessorRecruitingAgent:
	def __init__(
		self,
		db_path: str | Path = 'professor_recruiting.db',
		user_agent: str = 'search-for-prof-bot/1.0 (+https://github.com/gold3fluoride/search-for-prof)',
		rate_limit_seconds: float = 2.0,
		max_depth: int = 2,
		page_cap: int = 10,
	) -> None:
		assert max_depth >= 0
		assert page_cap > 0
		self.db_path = Path(db_path)
		self.session = requests.Session()
		self.session.headers.update({'User-Agent': user_agent})
		self.rate_limit_seconds = rate_limit_seconds
		self.max_depth = max_depth
		self.page_cap = page_cap
		self._last_domain_hit: dict[str, float] = {}
		self._robots_cache: dict[str, robotparser.RobotFileParser] = {}
		self._init_db()

	def _init_db(self) -> None:
		with sqlite3.connect(self.db_path) as conn:
			conn.executescript(
				'''
				CREATE TABLE IF NOT EXISTS runs (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					request_json TEXT NOT NULL,
					created_at TEXT NOT NULL
				);
				CREATE TABLE IF NOT EXISTS professors (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					name TEXT NOT NULL,
					institution TEXT NOT NULL,
					profile_url TEXT NOT NULL,
					homepage_url TEXT,
					lab_url TEXT
				);
				CREATE TABLE IF NOT EXISTS pages_visited (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					professor_id INTEGER NOT NULL,
					url TEXT NOT NULL,
					title TEXT,
					visited_at TEXT NOT NULL
				);
				CREATE TABLE IF NOT EXISTS recruiting_evidence (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					professor_id INTEGER NOT NULL,
					status TEXT NOT NULL,
					confidence REAL NOT NULL,
					evidence_text TEXT NOT NULL,
					evidence_url TEXT NOT NULL,
					checked_at TEXT NOT NULL
				);
				CREATE TABLE IF NOT EXISTS results (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					run_id INTEGER NOT NULL,
					professor_id INTEGER NOT NULL,
					fit_score REAL NOT NULL,
					status TEXT NOT NULL,
					output_json TEXT NOT NULL
				);
				'''
			)

	def _normalize_url(self, url: str) -> str:
		parsed = urlparse(url.strip())
		clean_query = urlencode([(k, v) for k, vs in parse_qs(parsed.query).items() for v in vs if not k.startswith('utm_')], doseq=True)
		return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', clean_query, ''))

	def _respect_rate_limit(self, url: str) -> None:
		domain = urlparse(url).netloc.lower()
		now = time.time()
		last = self._last_domain_hit.get(domain)
		if last is not None:
			wait_for = self.rate_limit_seconds - (now - last)
			if wait_for > 0:
				time.sleep(wait_for)
		self._last_domain_hit[domain] = time.time()

	def _is_allowed_by_robots(self, url: str) -> bool:
		parsed = urlparse(url)
		root = f'{parsed.scheme}://{parsed.netloc}'
		if root not in self._robots_cache:
			rp = robotparser.RobotFileParser()
			rp.set_url(urljoin(root, '/robots.txt'))
			try:
				rp.read()
			except Exception:
				return True
			self._robots_cache[root] = rp
		return self._robots_cache[root].can_fetch(self.session.headers['User-Agent'], url)

	def _fetch_html(self, url: str) -> str | None:
		if not self._is_allowed_by_robots(url):
			return None
		self._respect_rate_limit(url)
		try:
			resp = self.session.get(url, timeout=15)
			resp.raise_for_status()
		except Exception:
			return None
		return resp.text

	def _search_urls(self, query: str, cap: int = 8) -> list[str]:
		url = 'https://duckduckgo.com/html/'
		try:
			self._respect_rate_limit(url)
			resp = self.session.get(url, params={'q': query}, timeout=15)
			resp.raise_for_status()
		except Exception:
			return []
		soup = BeautifulSoup(resp.text, 'lxml')
		links: list[str] = []
		for tag in soup.select('a.result__a'):
			href = tag.get('href')
			if not href:
				continue
			links.append(self._normalize_url(href))
			if len(links) >= cap:
				break
		return links

	def discover_institution_pages(self, institution_name: str) -> list[str]:
		if institution_name.startswith(('http://', 'https://')):
			return [self._normalize_url(institution_name)]
		query = f'{institution_name} computer science faculty directory'
		candidates = self._search_urls(query)
		filtered = [
			u
			for u in candidates
			if any(key in u.lower() for key in ('faculty', 'people', 'directory', 'cs.', '/cs', 'computer-science', '.edu'))
		]
		return filtered[:5]

	def _parse_faculty_links(self, institution: str, page_url: str, html: str) -> list[ProfessorSource]:
		soup = BeautifulSoup(html, 'lxml')
		sources: list[ProfessorSource] = []
		for a_tag in soup.find_all('a', href=True):
			name = ' '.join(a_tag.get_text(' ', strip=True).split())
			if len(name.split()) < 2 or len(name.split()) > 5:
				continue
			href = self._normalize_url(urljoin(page_url, a_tag['href']))
			if not href.startswith(('http://', 'https://')):
				continue
			if not any(k in href.lower() for k in ('faculty', 'people', 'person', 'profile', '~', '/lab')):
				continue
			sources.append(ProfessorSource(name=name, institution=institution, profile_url=href))
		seen: set[str] = set()
		unique_sources: list[ProfessorSource] = []
		for source in sources:
			if source.profile_url in seen:
				continue
			seen.add(source.profile_url)
			unique_sources.append(source)
		return unique_sources[:50]

	def _resolve_professor_urls(self, source: ProfessorSource) -> ProfessorSource:
		html = self._fetch_html(source.profile_url)
		if not html:
			return source
		soup = BeautifulSoup(html, 'lxml')
		for a_tag in soup.find_all('a', href=True):
			href = self._normalize_url(urljoin(source.profile_url, a_tag['href']))
			text = a_tag.get_text(' ', strip=True).lower()
			if source.homepage_url is None and ('homepage' in text or 'website' in text):
				source.homepage_url = href
			if source.lab_url is None and 'lab' in text:
				source.lab_url = href
		return source

	def _score_link(self, url: str, text: str) -> int:
		combined = f'{url} {text}'.lower()
		return sum(1 for kw in RELEVANT_LINK_HINTS if kw in combined)

	def _extract_research_areas(self, text: str) -> list[str]:
		content = text.lower()
		return sorted([kw for kw in RESEARCH_KEYWORDS if kw in content])

	def _llm_extract(self, text: str, degree_level: str | None) -> tuple[RecruitingStatus, float, str] | None:
		api_key = (Path('.env').exists() and None)  # satisfy static lint expectations for env fallback path
		_ = api_key
		try:
			from openai import OpenAI
		except Exception:
			return None
		import os

		if not os.getenv('OPENAI_API_KEY'):
			return None
		client = OpenAI()
		prompt = {
			'degree_level': degree_level,
			'instruction': 'Return JSON with keys: recruiting_status(open|closed|unclear), confidence(0-1), evidence_quote.',
			'text': text[:5000],
		}
		try:
			resp = client.responses.create(
				model=os.getenv('OPENAI_MODEL', 'gpt-4o-mini'),
				input=json.dumps(prompt),
				max_output_tokens=250,
			)
			raw = resp.output_text.strip()
			data = json.loads(raw)
			status = data.get('recruiting_status', 'unclear')
			conf = float(data.get('confidence', 0.5))
			quote = str(data.get('evidence_quote', '')).strip()
			if status in {'open', 'closed', 'unclear'} and 0 <= conf <= 1 and quote:
				return status, conf, quote
		except Exception:
			return None
		return None

	def _regex_extract(self, text: str, degree_level: str | None) -> tuple[RecruitingStatus, float, str]:
		normalized = ' '.join(text.split())
		clauses = re.split(r'(?<=[.!?])\s+', normalized)
		for sentence in clauses:
			s = sentence.lower()
			if any(kw in s for kw in ('not taking students', 'not recruiting', 'not accepting students', 'no openings')):
				return 'closed', 0.95, sentence[:300]
		for sentence in clauses:
			s = sentence.lower()
			if any(kw in s for kw in ('accepting new students', 'open positions', 'looking for phd', 'prospective students welcome')):
				return 'open', 0.9, sentence[:300]
		return 'unclear', 0.45, 'No explicit recruiting statement found.'

	def extract_recruiting_evidence(self, text: str, degree_level: str | None) -> tuple[RecruitingStatus, float, str]:
		llm_result = self._llm_extract(text=text, degree_level=degree_level)
		if llm_result is not None:
			return llm_result
		return self._regex_extract(text=text, degree_level=degree_level)

	def _interest_similarity(self, interests: list[str], research_areas: list[str]) -> float:
		if not interests or not research_areas:
			return 0.0
		interest_tokens = {x.strip().lower() for x in interests}
		research_tokens = {x.strip().lower() for x in research_areas}
		overlap = len(interest_tokens & research_tokens)
		return overlap / max(len(interest_tokens), 1)

	def _compute_fit(self, interests: list[str], research_areas: list[str], status: RecruitingStatus, confidence: float) -> float:
		interest_similarity = self._interest_similarity(interests, research_areas)
		recruiting_component = confidence if status == 'open' else 0.0
		score = 0.6 * interest_similarity + 0.4 * recruiting_component
		if status == 'closed':
			score *= 0.2
		return round(min(max(score, 0.0), 1.0), 4)

	def _allowed_domain_set(self, source: ProfessorSource) -> set[str]:
		domains = {urlparse(source.profile_url).netloc.lower()}
		if source.homepage_url:
			domains.add(urlparse(source.homepage_url).netloc.lower())
		if source.lab_url:
			domains.add(urlparse(source.lab_url).netloc.lower())
		return {d for d in domains if d}

	def _crawl_professor(self, source: ProfessorSource, degree_level: str | None) -> tuple[RecruitingStatus, float, str, str, list[str], list[tuple[str, str]]]:
		start_urls = [x for x in [source.homepage_url, source.lab_url, source.profile_url] if x]
		queue: deque[tuple[str, int]] = deque((url, 0) for url in start_urls)
		allowed_domains = self._allowed_domain_set(source)
		visited: set[str] = set()
		pages_visited: list[tuple[str, str]] = []
		aggregated_research: set[str] = set()
		best: tuple[RecruitingStatus, float, str, str] = ('unclear', 0.0, 'No pages visited.', source.profile_url)

		while queue and len(visited) < self.page_cap:
			url, depth = queue.popleft()
			url = self._normalize_url(url)
			if url in visited or depth > self.max_depth:
				continue
			if urlparse(url).netloc.lower() not in allowed_domains:
				continue
			html = self._fetch_html(url)
			if html is None:
				continue
			visited.add(url)
			soup = BeautifulSoup(html, 'lxml')
			text = soup.get_text(' ', strip=True)
			title = soup.title.get_text(strip=True) if soup.title else ''
			pages_visited.append((url, title))
			aggregated_research.update(self._extract_research_areas(text))
			status, confidence, evidence = self.extract_recruiting_evidence(text=text, degree_level=degree_level)
			if confidence > best[1]:
				best = (status, confidence, evidence, url)
			if status in {'open', 'closed'} and confidence >= 0.85:
				break
			if depth == self.max_depth:
				continue
			candidates: list[tuple[int, str]] = []
			for a_tag in soup.find_all('a', href=True):
				next_url = self._normalize_url(urljoin(url, a_tag['href']))
				if not next_url.startswith(('http://', 'https://')):
					continue
				score = self._score_link(next_url, a_tag.get_text(' ', strip=True))
				if score > 0:
					candidates.append((score, next_url))
			candidates.sort(reverse=True, key=lambda x: x[0])
			for _, next_url in candidates[:5]:
				if next_url not in visited:
					queue.append((next_url, depth + 1))
		return best[0], best[1], best[2], best[3], sorted(aggregated_research), pages_visited

	def _insert_professor(self, conn: sqlite3.Connection, source: ProfessorSource) -> int:
		cursor = conn.execute(
			'INSERT INTO professors(name, institution, profile_url, homepage_url, lab_url) VALUES (?, ?, ?, ?, ?)',
			(source.name, source.institution, source.profile_url, source.homepage_url, source.lab_url),
		)
		return int(cursor.lastrowid)

	def run(self, request: Phase1Input) -> list[Phase1Result]:
		assert request.interests
		now = datetime.now(UTC).isoformat()
		with sqlite3.connect(self.db_path) as conn:
			run_id = int(
				conn.execute('INSERT INTO runs(request_json, created_at) VALUES (?, ?)', (request.model_dump_json(), now)).lastrowid
			)
			sources: list[ProfessorSource] = []
			for institution in request.target_institutions:
				for inst_page in self.discover_institution_pages(institution):
					html = self._fetch_html(inst_page)
					if html is None:
						continue
					sources.extend(self._parse_faculty_links(institution=institution, page_url=inst_page, html=html))
			unique_sources: dict[str, ProfessorSource] = {}
			for source in sources:
				unique_sources[source.profile_url] = source
			results: list[Phase1Result] = []
			for source in unique_sources.values():
				source = self._resolve_professor_urls(source)
				professor_id = self._insert_professor(conn, source)
				status, confidence, evidence, evidence_url, research_areas, visited = self._crawl_professor(
					source=source, degree_level=request.degree_level
				)
				for page_url, title in visited:
					conn.execute(
						'INSERT INTO pages_visited(professor_id, url, title, visited_at) VALUES (?, ?, ?, ?)',
						(professor_id, page_url, title, now),
					)
				conn.execute(
					'''
					INSERT INTO recruiting_evidence(professor_id, status, confidence, evidence_text, evidence_url, checked_at)
					VALUES (?, ?, ?, ?, ?, ?)
					''',
					(professor_id, status, confidence, evidence, evidence_url, now),
				)
				fit_score = self._compute_fit(request.interests, research_areas, status, confidence)
				result = Phase1Result(
					name=source.name,
					institution=source.institution,
					lab=source.lab_url,
					research_areas=research_areas,
					recruiting_status=status,
					status_confidence=round(confidence, 4),
					fit_score=fit_score,
					evidence_text=evidence,
					evidence_url=evidence_url,
					checked_at=now,
				)
				results.append(result)
				conn.execute(
					'INSERT INTO results(run_id, professor_id, fit_score, status, output_json) VALUES (?, ?, ?, ?, ?)',
					(run_id, professor_id, fit_score, status, result.model_dump_json()),
				)
			results.sort(key=lambda x: x.fit_score, reverse=True)
		return results


def _render_terminal_table(results: list[Phase1Result]) -> str:
	if not results:
		return 'No matching professors found.'
	header = f'{"Name":28} {"Institution":28} {"Status":10} {"Conf":6} {"Fit":6} Evidence'
	lines = [header, '-' * len(header)]
	for row in results:
		lines.append(
			f'{row.name[:27]:28} {row.institution[:27]:28} {row.recruiting_status.upper():10} {row.status_confidence:<6.2f} {row.fit_score:<6.2f} {row.evidence_url}'
		)
	return '\n'.join(lines)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Phase 1 professor recruiting matcher (text input only).')
	parser.add_argument('--interests', required=True, help='Comma-separated interests, e.g. "NLP, systems, robotics"')
	parser.add_argument('--institution', action='append', required=True, dest='institutions', help='Institution name or faculty URL')
	parser.add_argument('--degree', choices=['intern', 'masters', 'phd'], default=None)
	parser.add_argument('--start-term', default=None)
	parser.add_argument('--notes', default=None)
	parser.add_argument('--db-path', default='professor_recruiting.db')
	parser.add_argument('--output-json', default='phase1_results.json')
	return parser.parse_args()


def run_from_cli() -> int:
	args = parse_args()
	request = Phase1Input(
		interests=[x.strip() for x in args.interests.split(',') if x.strip()],
		target_institutions=args.institutions,
		degree_level=args.degree,
		start_term=args.start_term,
		notes=args.notes,
	)
	agent = ProfessorRecruitingAgent(db_path=args.db_path)
	results = agent.run(request)
	print(_render_terminal_table(results))
	with Path(args.output_json).open('w', encoding='utf-8') as handle:
		handle.write(json.dumps([r.model_dump() for r in results], indent=2))
	print(f'\nWrote JSON results to {args.output_json}')
	return 0
