from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib import robotparser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from browser_use.professor_search.models import (
	DegreeLevel,
	Phase1Input,
	Phase1Result,
	Phase1RunOutput,
	ProfessorCandidate,
	RecruitingEvidence,
	RecruitingStatus,
)

RELEVANT_LINK_HINTS = ('prospective', 'join', 'openings', 'positions', 'phd', 'intern', 'students')
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
		config_path: str | Path | None = None,
	) -> None:
		self.phase1_config = self._load_phase1_config(config_path)
		self.db_path = Path(db_path)
		self.session = requests.Session()
		self.session.headers.update({'User-Agent': user_agent})
		self.rate_limit_seconds = rate_limit_seconds
		self.max_depth = int(self.phase1_config['crawl'].get('max_depth', max_depth))
		self.page_cap = int(self.phase1_config['crawl'].get('max_pages_per_professor', page_cap))
		self.request_timeout_seconds = float(self.phase1_config['crawl'].get('timeout_seconds', 15))
		self.max_retries = int(self.phase1_config['crawl'].get('retries', 2))
		self.backoff_seconds = float(self.phase1_config['crawl'].get('backoff_seconds', 1.5))
		self.link_priority_keywords = tuple(self.phase1_config['crawl'].get('link_priority_keywords', RELEVANT_LINK_HINTS))
		self.interest_weight = float(self.phase1_config['scoring'].get('interest_weight', 0.65))
		self.recruiting_weight = float(self.phase1_config['scoring'].get('recruiting_weight', 0.35))
		self.unclear_recruiting_component = float(self.phase1_config['scoring'].get('unclear_recruiting_component', 0.2))
		self.closed_penalty = float(self.phase1_config['scoring'].get('closed_penalty', 0.1))
		self._last_domain_hit: dict[str, float] = {}
		self._robots_cache: dict[str, robotparser.RobotFileParser] = {}
		self.fetch_log: list[tuple[str, str, str, bool]] = []
		self._init_db()

	def _load_phase1_config(self, config_path: str | Path | None) -> dict:
		defaults = {
			'crawl': {
				'max_depth': 2,
				'max_pages_per_professor': 10,
				'timeout_seconds': 15,
				'retries': 2,
				'backoff_seconds': 1.5,
				'link_priority_keywords': list(RELEVANT_LINK_HINTS),
			},
			'scoring': {
				'interest_weight': 0.65,
				'recruiting_weight': 0.35,
				'unclear_recruiting_component': 0.2,
				'closed_penalty': 0.1,
			},
			'model': {'provider': 'openai', 'name': 'gpt-4o-mini'},
		}
		path = Path(config_path) if config_path else Path(__file__).with_name('phase1_config.yaml')
		if not path.exists():
			return defaults
		try:
			import yaml
		except Exception:
			return defaults
		try:
			with path.open(encoding='utf-8') as handle:
				loaded = yaml.safe_load(handle) or {}
		except Exception:
			return defaults
		for section, values in defaults.items():
			loaded.setdefault(section, {})
			for key, val in values.items():
				loaded[section].setdefault(key, val)
		return loaded

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
					reason TEXT,
					visited_at TEXT NOT NULL
				);
				CREATE TABLE IF NOT EXISTS evidence (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					professor_id INTEGER NOT NULL,
					status TEXT NOT NULL,
					confidence REAL NOT NULL,
					evidence_text TEXT NOT NULL,
					evidence_url TEXT NOT NULL,
					applies_to TEXT,
					checked_at TEXT NOT NULL
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
					rank INTEGER NOT NULL,
					fit_score REAL NOT NULL,
					status TEXT NOT NULL,
					output_json TEXT NOT NULL
				);
				'''
			)
			for ddl in (
				'ALTER TABLE pages_visited ADD COLUMN reason TEXT',
				'ALTER TABLE results ADD COLUMN rank INTEGER DEFAULT 0',
			):
				try:
					conn.execute(ddl)
				except sqlite3.OperationalError:
					pass

	def _normalize_url(self, url: str) -> str:
		parsed = urlparse(url.strip())
		filtered_params: list[tuple[str, str]] = []
		for key, values in parse_qs(parsed.query).items():
			if key.startswith('utm_'):
				continue
			for value in values:
				filtered_params.append((key, value))
		clean_query = urlencode(filtered_params, doseq=True)
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

	def _fetch_html(self, url: str, reason: str = 'crawl') -> str | None:
		if not self._is_allowed_by_robots(url):
			self.fetch_log.append((datetime.now(UTC).isoformat(), url, reason, False))
			return None
		for attempt in range(self.max_retries + 1):
			self._respect_rate_limit(url)
			try:
				resp = self.session.get(url, timeout=self.request_timeout_seconds)
				resp.raise_for_status()
				self.fetch_log.append((datetime.now(UTC).isoformat(), url, reason, True))
				return resp.text
			except Exception:
				if attempt >= self.max_retries:
					break
				time.sleep(self.backoff_seconds * (attempt + 1))
		self.fetch_log.append((datetime.now(UTC).isoformat(), url, reason, False))
		return None

	def _search_urls(self, query: str, cap: int = 8) -> list[str]:
		url = 'https://duckduckgo.com/html/'
		try:
			self._respect_rate_limit(url)
			resp = self.session.get(url, params={'q': query}, timeout=self.request_timeout_seconds)
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

	def discover_faculty_pages(self, institution: str) -> list[str]:
		if institution.startswith(('http://', 'https://')):
			return [self._normalize_url(institution)]
		query = f'{institution} computer science faculty directory'
		candidates = self._search_urls(query)
		filtered = [
			url
			for url in candidates
			if any(key in url.lower() for key in ('faculty', 'people', 'directory', 'cs.', '/cs', 'computer-science', '.edu'))
		]
		return filtered[:5]

	def discover_institution_pages(self, institution_name: str) -> list[str]:
		return self.discover_faculty_pages(institution_name)

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
			if not any(key in href.lower() for key in ('faculty', 'people', 'person', 'profile', '~', '/lab')):
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

	def resolve_professor_sites(self, profile_url: str) -> ProfessorCandidate:
		html = self._fetch_html(profile_url, reason='resolve_professor_sites')
		candidate = ProfessorCandidate(name='', institution='', profile_url=profile_url)
		if not html:
			return candidate
		soup = BeautifulSoup(html, 'lxml')
		for a_tag in soup.find_all('a', href=True):
			href = self._normalize_url(urljoin(profile_url, a_tag['href']))
			text = a_tag.get_text(' ', strip=True).lower()
			if candidate.homepage_url is None and ('homepage' in text or 'website' in text):
				candidate.homepage_url = href
			if candidate.lab_url is None and 'lab' in text:
				candidate.lab_url = href
		return candidate

	def _resolve_professor_urls(self, source: ProfessorSource) -> ProfessorSource:
		candidate = self.resolve_professor_sites(source.profile_url)
		source.homepage_url = candidate.homepage_url
		source.lab_url = candidate.lab_url
		return source

	def _score_link(self, url: str, text: str) -> int:
		combined = f'{url} {text}'.lower()
		return sum(1 for keyword in self.link_priority_keywords if keyword in combined)

	def extract_research_areas(self, page_text: str) -> list[str]:
		content = page_text.lower()
		return sorted([kw for kw in RESEARCH_KEYWORDS if kw in content])

	def _extract_research_areas(self, text: str) -> list[str]:
		return self.extract_research_areas(text)

	def _detect_applies_to(self, text: str) -> DegreeLevel | None:
		lowered = text.lower()
		if 'phd' in lowered or 'doctor' in lowered:
			return DegreeLevel.PHD
		if 'intern' in lowered:
			return DegreeLevel.INTERN
		if any(token in lowered for token in ('master', 'masters', 'ms')):
			return DegreeLevel.MASTERS
		return None

	def _llm_extract(self, text: str, url: str, degree_level: str | None, checked_at: str) -> RecruitingEvidence | None:
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
			'instruction': 'Return STRICT JSON with keys: status(open|closed|unclear), confidence(0-1), evidence_text(exact quote), evidence_url, applies_to(phd|intern|masters|null).',
			'url': url,
			'text': text[:3000],
		}
		try:
			resp = client.responses.create(
				model=os.getenv('OPENAI_MODEL', str(self.phase1_config['model'].get('name', 'gpt-4o-mini'))),
				input=json.dumps(prompt),
				max_output_tokens=300,
			)
			data = json.loads(resp.output_text.strip())
			status = RecruitingStatus(data.get('status', 'unclear'))
			confidence = float(data.get('confidence', 0.5))
			evidence_text = str(data.get('evidence_text', '')).strip()
			applies_to = data.get('applies_to')
			if not evidence_text or confidence < 0 or confidence > 1:
				return None
			return RecruitingEvidence(
				status=status,
				confidence=confidence,
				evidence_text=evidence_text,
				evidence_url=str(data.get('evidence_url') or url),
				applies_to=DegreeLevel(applies_to) if applies_to in {'phd', 'intern', 'masters'} else None,
				checked_at=checked_at,
			)
		except Exception:
			return None

	def _regex_extract(self, text: str, url: str, checked_at: str) -> RecruitingEvidence | None:
		normalized = ' '.join(text.split())
		sentences = re.split(r'(?<=[.!?])\s+', normalized)
		for sentence in sentences:
			lowered = sentence.lower()
			if any(token in lowered for token in ('not taking students', 'not recruiting', 'not accepting students', 'no openings')):
				return RecruitingEvidence(
					status=RecruitingStatus.CLOSED,
					confidence=0.95,
					evidence_text=sentence[:300],
					evidence_url=url,
					applies_to=self._detect_applies_to(sentence),
					checked_at=checked_at,
				)
		for sentence in sentences:
			lowered = sentence.lower()
			if any(token in lowered for token in ('accepting new students', 'open positions', 'looking for phd', 'prospective students welcome')):
				return RecruitingEvidence(
					status=RecruitingStatus.OPEN,
					confidence=0.9,
					evidence_text=sentence[:300],
					evidence_url=url,
					applies_to=self._detect_applies_to(sentence),
					checked_at=checked_at,
				)
		return None

	def _candidate_recruiting_sentences(self, page_text: str) -> list[str]:
		normalized = ' '.join(page_text.split())
		sentences = re.split(r'(?<=[.!?])\s+', normalized)
		keywords = ('recruit', 'students', 'prospective', 'openings', 'positions', 'phd', 'intern', 'join', 'admission')
		return [sentence for sentence in sentences if any(keyword in sentence.lower() for keyword in keywords)]

	def _is_recent_signal(self, text: str) -> bool:
		lowered = text.lower()
		if any(token in lowered for token in ('fall', 'spring', 'this year', 'upcoming', 'currently')):
			return True
		years = [int(match) for match in re.findall(r'20\d{2}', text)]
		return bool(years and max(years) >= datetime.now(UTC).year - 1)

	def _looks_stale(self, url: str, text: str) -> bool:
		if any(token in url.lower() for token in ('/news', '/blog', '/archive')):
			return True
		years = [int(match) for match in re.findall(r'20\d{2}', text)]
		return bool(years and max(years) <= datetime.now(UTC).year - 2)

	def _calibrate_confidence(self, evidence: RecruitingEvidence) -> RecruitingEvidence:
		confidence = evidence.confidence
		if self._is_recent_signal(evidence.evidence_text):
			confidence = min(1.0, confidence + 0.05)
		if self._looks_stale(evidence.evidence_url, evidence.evidence_text):
			confidence = max(0.0, confidence - 0.2)
		return evidence.model_copy(update={'confidence': round(confidence, 4)})

	def extract_recruiting_signals(self, page_text: str, url: str, degree_level: str | None = None) -> RecruitingEvidence:
		checked_at = datetime.now(UTC).isoformat()
		snippets = self._candidate_recruiting_sentences(page_text) or [page_text[:1200]]
		signals: list[RecruitingEvidence] = []
		for snippet in snippets[:6]:
			regex_signal = self._regex_extract(snippet, url=url, checked_at=checked_at)
			if regex_signal:
				signals.append(self._calibrate_confidence(regex_signal))
				continue
			llm_signal = self._llm_extract(snippet, url=url, degree_level=degree_level, checked_at=checked_at)
			if llm_signal:
				signals.append(self._calibrate_confidence(llm_signal))
		if not signals:
			return RecruitingEvidence(
				status=RecruitingStatus.UNCLEAR,
				confidence=0.45,
				evidence_text='No explicit recruiting statement found.',
				evidence_url=url,
				applies_to=DegreeLevel(degree_level) if degree_level in {'intern', 'masters', 'phd'} else None,
				checked_at=checked_at,
			)
		signals.sort(key=lambda signal: signal.confidence, reverse=True)
		has_open = any(signal.status == RecruitingStatus.OPEN for signal in signals)
		has_closed = any(signal.status == RecruitingStatus.CLOSED for signal in signals)
		if has_open and has_closed:
			recent_explicit = [signal for signal in signals if signal.confidence >= 0.9 and self._is_recent_signal(signal.evidence_text)]
			if recent_explicit:
				return recent_explicit[0]
			best = signals[0]
			return best.model_copy(update={'status': RecruitingStatus.UNCLEAR, 'confidence': min(0.6, best.confidence)})
		return signals[0]

	def extract_recruiting_evidence(self, text: str, degree_level: str | None) -> tuple[str, float, str]:
		evidence = self.extract_recruiting_signals(page_text=text, url='https://local.test/evidence', degree_level=degree_level)
		return evidence.status.value, evidence.confidence, evidence.evidence_text

	def _interest_similarity(self, interests: list[str], research_areas: list[str]) -> float:
		if not interests or not research_areas:
			return 0.0
		interest_tokens = {item.strip().lower() for item in interests}
		research_tokens = {item.strip().lower() for item in research_areas}
		return len(interest_tokens & research_tokens) / max(len(interest_tokens), 1)

	def compute_fit_score(
		self,
		interests: list[str],
		research_areas: list[str],
		status_confidence: float,
		status: RecruitingStatus,
	) -> float:
		interest_similarity = self._interest_similarity(interests, research_areas)
		if status == RecruitingStatus.OPEN:
			recruiting_component = status_confidence
		elif status == RecruitingStatus.UNCLEAR:
			recruiting_component = self.unclear_recruiting_component
		else:
			recruiting_component = self.closed_penalty
		score = self.interest_weight * interest_similarity + self.recruiting_weight * recruiting_component
		if status == RecruitingStatus.CLOSED:
			score *= self.closed_penalty
		return round(min(max(score, 0.0), 1.0), 4)

	def _compute_fit(self, interests: list[str], research_areas: list[str], status: str, confidence: float) -> float:
		return self.compute_fit_score(interests, research_areas, confidence, RecruitingStatus(status))

	def rank_results(self, results: list[Phase1Result]) -> list[Phase1Result]:
		return sorted(results, key=lambda item: item.fit_score, reverse=True)

	def _allowed_domain_set(self, source: ProfessorSource) -> set[str]:
		domains = {urlparse(source.profile_url).netloc.lower()}
		if source.homepage_url:
			domains.add(urlparse(source.homepage_url).netloc.lower())
		if source.lab_url:
			domains.add(urlparse(source.lab_url).netloc.lower())
		return {domain for domain in domains if domain}

	def crawl_professor_pages(
		self,
		seed_urls: list[str],
		limits: dict[str, int],
		allowed_domains: set[str],
		degree_level: str | None,
	) -> tuple[RecruitingEvidence, list[str], list[tuple[str, str, str]]]:
		max_depth = min(int(limits.get('max_depth', self.max_depth)), self.max_depth)
		page_cap = min(int(limits.get('page_cap', self.page_cap)), self.page_cap)
		queue: deque[tuple[str, int, str]] = deque((url, 0, 'seed') for url in seed_urls)
		visited: set[str] = set()
		pages_visited: list[tuple[str, str, str]] = []
		aggregated_research: set[str] = set()
		best = RecruitingEvidence(
			status=RecruitingStatus.UNCLEAR,
			confidence=0.0,
			evidence_text='No pages visited.',
			evidence_url=seed_urls[0] if seed_urls else '',
			checked_at=datetime.now(UTC).isoformat(),
		)
		while queue and len(visited) < page_cap:
			url, depth, reason = queue.popleft()
			url = self._normalize_url(url)
			if url in visited or depth > max_depth:
				continue
			if urlparse(url).netloc.lower() not in allowed_domains:
				continue
			html = self._fetch_html(url, reason=reason)
			if html is None:
				continue
			visited.add(url)
			soup = BeautifulSoup(html, 'lxml')
			text = soup.get_text(' ', strip=True)
			title = soup.title.get_text(strip=True) if soup.title else ''
			pages_visited.append((url, title, reason))
			aggregated_research.update(self.extract_research_areas(text))
			evidence = self.extract_recruiting_signals(page_text=text, url=url, degree_level=degree_level)
			if evidence.confidence > best.confidence:
				best = evidence
			if evidence.status in {RecruitingStatus.OPEN, RecruitingStatus.CLOSED} and evidence.confidence >= 0.85:
				break
			if depth == max_depth:
				continue
			candidates: list[tuple[int, str]] = []
			for a_tag in soup.find_all('a', href=True):
				next_url = self._normalize_url(urljoin(url, a_tag['href']))
				if not next_url.startswith(('http://', 'https://')):
					continue
				score = self._score_link(next_url, a_tag.get_text(' ', strip=True))
				if score > 0:
					candidates.append((score, next_url))
			candidates.sort(reverse=True, key=lambda item: item[0])
			for _, next_url in candidates[:5]:
				if next_url not in visited:
					queue.append((next_url, depth + 1, 'priority_link'))
		return best, sorted(aggregated_research), pages_visited

	def _crawl_professor(self, source: ProfessorSource, degree_level: str | None) -> tuple[str, float, str, str, list[str], list[tuple[str, str]]]:
		evidence, research, visited = self.crawl_professor_pages(
			seed_urls=[url for url in [source.homepage_url, source.lab_url, source.profile_url] if url],
			limits={'max_depth': self.max_depth, 'page_cap': self.page_cap},
			allowed_domains=self._allowed_domain_set(source),
			degree_level=degree_level,
		)
		return evidence.status.value, evidence.confidence, evidence.evidence_text, evidence.evidence_url, research, [
			(url, title) for url, title, _ in visited
		]

	def _insert_professor(self, conn: sqlite3.Connection, source: ProfessorSource) -> int:
		cursor = conn.execute(
			'INSERT INTO professors(name, institution, profile_url, homepage_url, lab_url) VALUES (?, ?, ?, ?, ?)',
			(source.name, source.institution, source.profile_url, source.homepage_url, source.lab_url),
		)
		return int(cursor.lastrowid)

	def run_with_metadata(self, request: Phase1Input) -> Phase1RunOutput:
		if not request.interests:
			raise ValueError('interests list cannot be empty')
		now = datetime.now(UTC).isoformat()
		with sqlite3.connect(self.db_path) as conn:
			run_id = int(conn.execute('INSERT INTO runs(request_json, created_at) VALUES (?, ?)', (request.model_dump_json(), now)).lastrowid)
			sources: list[ProfessorSource] = []
			for institution in request.target_institutions:
				for inst_page in self.discover_faculty_pages(institution):
					html = self._fetch_html(inst_page, reason='discover_faculty_pages')
					if html is None:
						continue
					sources.extend(self._parse_faculty_links(institution=institution, page_url=inst_page, html=html))
			unique_sources: dict[str, ProfessorSource] = {source.profile_url: source for source in sources}
			selected_sources = list(unique_sources.values())[: request.max_professors] if request.max_professors else list(unique_sources.values())
			results: list[tuple[int, Phase1Result]] = []
			for source in selected_sources:
				source = self._resolve_professor_urls(source)
				professor_id = self._insert_professor(conn, source)
				evidence, research_areas, visited = self.crawl_professor_pages(
					seed_urls=[url for url in [source.homepage_url, source.lab_url, source.profile_url] if url],
					limits={'max_depth': self.max_depth, 'page_cap': self.page_cap},
					allowed_domains=self._allowed_domain_set(source),
					degree_level=request.degree_level.value if request.degree_level else None,
				)
				for page_url, title, reason in visited:
					conn.execute(
						'INSERT INTO pages_visited(professor_id, url, title, reason, visited_at) VALUES (?, ?, ?, ?, ?)',
						(professor_id, page_url, title, reason, evidence.checked_at),
					)
				conn.execute(
					'''
					INSERT INTO evidence(professor_id, status, confidence, evidence_text, evidence_url, applies_to, checked_at)
					VALUES (?, ?, ?, ?, ?, ?, ?)
					''',
					(
						professor_id,
						evidence.status.value,
						evidence.confidence,
						evidence.evidence_text,
						evidence.evidence_url,
						evidence.applies_to.value if evidence.applies_to else None,
						evidence.checked_at,
					),
				)
				conn.execute(
					'''
					INSERT INTO recruiting_evidence(professor_id, status, confidence, evidence_text, evidence_url, checked_at)
					VALUES (?, ?, ?, ?, ?, ?)
					''',
					(professor_id, evidence.status.value, evidence.confidence, evidence.evidence_text, evidence.evidence_url, evidence.checked_at),
				)
				fit_score = self.compute_fit_score(request.interests, research_areas, evidence.confidence, evidence.status)
				result = Phase1Result(
					name=source.name,
					institution=source.institution,
					lab=source.lab_url,
					research_areas=research_areas,
					recruiting_status=evidence.status,
					status_confidence=round(evidence.confidence, 4),
					fit_score=fit_score,
					evidence_text=evidence.evidence_text,
					evidence_url=evidence.evidence_url,
					applies_to=evidence.applies_to,
					checked_at=evidence.checked_at,
				)
				results.append((professor_id, result))
			ranked = self.rank_results([result for _, result in results])
			for rank_index, ranked_result in enumerate(ranked, start=1):
				professor_id = next(pid for pid, result in results if result.name == ranked_result.name and result.institution == ranked_result.institution)
				conn.execute(
					'INSERT INTO results(run_id, professor_id, rank, fit_score, status, output_json) VALUES (?, ?, ?, ?, ?, ?)',
					(
						run_id,
						professor_id,
						rank_index,
						ranked_result.fit_score,
						ranked_result.recruiting_status.value,
						ranked_result.model_dump_json(),
					),
				)
		return Phase1RunOutput(run_id=run_id, input_echo=request, results=ranked)

	def run(self, request: Phase1Input) -> list[Phase1Result]:
		return self.run_with_metadata(request).results


def _render_terminal_table(results: list[Phase1Result]) -> str:
	if not results:
		return 'No matching professors found.'
	header = f'{"Name":28} {"Institution":28} {"Status":10} {"Conf":6} {"Fit":6} Evidence'
	lines = [header, '-' * len(header)]
	for row in results:
		lines.append(
			f'{row.name[:27]:28} {row.institution[:27]:28} {row.recruiting_status.value.upper():10} {row.status_confidence:<6.2f} {row.fit_score:<6.2f} {row.evidence_url}'
		)
	return '\n'.join(lines)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Phase 1 professor recruiting matcher (text input only).')
	parser.add_argument('--interests', required=True, help='Comma-separated interests, e.g. "NLP, systems, robotics"')
	parser.add_argument('--institution', action='append', required=True, dest='institutions', help='Institution name or faculty URL')
	parser.add_argument('--degree', choices=['intern', 'masters', 'phd'], default=None)
	parser.add_argument('--start-term', default=None)
	parser.add_argument('--notes', default=None)
	parser.add_argument('--max-professors', type=int, default=None)
	parser.add_argument('--db-path', default='professor_recruiting.db')
	parser.add_argument('--output-json', default='phase1_results.json')
	parser.add_argument('--output-csv', default=None)
	return parser.parse_args()


def run_from_cli() -> int:
	args = parse_args()
	request = Phase1Input(
		interests=[item.strip() for item in args.interests.split(',') if item.strip()],
		target_institutions=args.institutions,
		degree_level=DegreeLevel(args.degree) if args.degree else None,
		max_professors=args.max_professors,
		start_term=args.start_term,
		notes=args.notes,
	)
	agent = ProfessorRecruitingAgent(db_path=args.db_path)
	payload = agent.run_with_metadata(request)
	print(_render_terminal_table(payload.results))
	with Path(args.output_json).open('w', encoding='utf-8') as handle:
		handle.write(json.dumps(payload.model_dump(), indent=2))
	print(f'\nWrote JSON results to {args.output_json}')
	if args.output_csv:
		with Path(args.output_csv).open('w', encoding='utf-8', newline='') as csv_handle:
			writer = csv.DictWriter(
				csv_handle,
				fieldnames=[
					'name',
					'institution',
					'recruiting_status',
					'status_confidence',
					'fit_score',
					'evidence_text',
					'evidence_url',
					'checked_at',
				],
			)
			writer.writeheader()
			for result in payload.results:
				writer.writerow(
					{
						'name': result.name,
						'institution': result.institution,
						'recruiting_status': result.recruiting_status.value,
						'status_confidence': result.status_confidence,
						'fit_score': result.fit_score,
						'evidence_text': result.evidence_text,
						'evidence_url': result.evidence_url,
						'checked_at': result.checked_at,
					}
				)
		print(f'Wrote CSV results to {args.output_csv}')
	return 0
