from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RecruitingStatus(StrEnum):
	OPEN = 'open'
	CLOSED = 'closed'
	UNCLEAR = 'unclear'


class DegreeLevel(StrEnum):
	INTERN = 'intern'
	MASTERS = 'masters'
	PHD = 'phd'


class Phase1Input(BaseModel):
	"""Phase-1 text-only input contract: interests + target institutions + optional constraints."""

	model_config = ConfigDict(extra='forbid')

	interests: list[str] = Field(min_length=1)
	target_institutions: list[str] = Field(min_length=1)
	degree_level: DegreeLevel | None = None
	max_professors: int | None = Field(default=None, ge=1)
	start_term: str | None = None
	notes: str | None = None


class ProfessorCandidate(BaseModel):
	model_config = ConfigDict(extra='forbid')

	name: str
	institution: str
	profile_url: str
	homepage_url: str | None = None
	lab_url: str | None = None


class RecruitingEvidence(BaseModel):
	model_config = ConfigDict(extra='forbid')

	status: RecruitingStatus
	confidence: float = Field(ge=0, le=1)
	evidence_text: str
	evidence_url: str
	applies_to: DegreeLevel | None = None
	checked_at: str


class Phase1Result(BaseModel):
	model_config = ConfigDict(extra='forbid')

	name: str
	institution: str
	lab: str | None = None
	research_areas: list[str] = Field(default_factory=list)
	recruiting_status: RecruitingStatus
	status_confidence: float = Field(ge=0, le=1)
	fit_score: float = Field(ge=0, le=1)
	evidence_text: str
	evidence_url: str
	applies_to: DegreeLevel | None = None
	checked_at: str


class Phase1RunOutput(BaseModel):
	model_config = ConfigDict(extra='forbid')

	run_id: int
	input_echo: Phase1Input
	results: list[Phase1Result]
