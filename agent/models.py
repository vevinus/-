from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Span(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("끝 시간은 시작 시간보다 커야 합니다.")
        return self


class Cut(Span):
    id: str
    kind: Literal["silence", "freeze"]
    selected: bool = True


class Caption(Span):
    text: str = Field(max_length=2000)


class Effect(BaseModel):
    time: float = Field(ge=0)
    kind: Literal["pop", "ding", "whoosh"]
    reason: str = Field(max_length=200)
    selected: bool = True
    volume: float = Field(default=0.22, ge=0, le=1)


class Settings(BaseModel):
    silence_db: float = Field(default=-35, ge=-65, le=-15)
    silence_duration: float = Field(default=0.7, ge=0.3, le=5)
    freeze_duration: float = Field(default=1.0, ge=0.3, le=10)
    padding: float = Field(default=0.15, ge=0, le=0.5)
    transcribe: bool = True
    model: Literal["tiny", "base", "small"] = "base"
    language: Literal["ko", "en", "auto"] = "ko"


class Edit(BaseModel):
    cuts: list[Cut] = Field(default_factory=list, max_length=10000)
    captions: list[Caption] = Field(default_factory=list, max_length=10000)
    effects: list[Effect] = Field(default_factory=list, max_length=10000)
