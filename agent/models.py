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
    needs_review: bool = False
    alignment_score: float | None = Field(default=None, ge=0, le=1)


class SpeechWord(Span):
    text: str


class Effect(BaseModel):
    time: float = Field(ge=0)
    kind: Literal["pop", "ding", "whoosh"]
    reason: str = Field(max_length=200)
    selected: bool = True
    volume: float = Field(default=0.22, ge=0, le=1)


class CaptionAppearance(BaseModel):
    size: float = Field(default=8, ge=4, le=40)
    y: float = Field(default=-.78, ge=-1, le=1)
    color: tuple[float, float, float] = (1, 1, 1)
    bold: bool = True
    italic: bool = False

    @model_validator(mode="after")
    def valid_color(self):
        if not all(0 <= value <= 1 for value in self.color):
            raise ValueError("자막 색상 값은 0~1 범위여야 합니다.")
        return self


class Settings(BaseModel):
    silence_db: float = Field(default=-35, ge=-65, le=-15)
    silence_duration: float = Field(default=0.7, ge=0.3, le=5)
    freeze_duration: float = Field(default=1.0, ge=0.3, le=10)
    padding: float = Field(default=0.15, ge=0, le=0.5)
    transcribe: bool = True
    model: Literal["tiny", "base", "small"] = "base"
    language: Literal["ko", "en", "auto"] = "ko"
    script: str = Field(default="", max_length=8000)
    style_id: str = Field(default="", max_length=32)
    caption_characters: int = Field(default=22, ge=6, le=80)
    caption_seconds: float = Field(default=3, ge=.5, le=8)

    @model_validator(mode="after")
    def script_requires_speech(self):
        self.script = self.script.strip()
        if self.script and not self.transcribe:
            raise ValueError("대본 시간 맞추기에는 자동 자막 생성을 켜야 합니다.")
        if self.script and not any(char.isalnum() for char in self.script):
            raise ValueError("대본에 실제로 말하는 문장을 입력하세요.")
        return self


class Edit(BaseModel):
    cuts: list[Cut] = Field(default_factory=list, max_length=10000)
    captions: list[Caption] = Field(default_factory=list, max_length=10000)
    effects: list[Effect] = Field(default_factory=list, max_length=10000)
    caption_appearance: CaptionAppearance = Field(default_factory=CaptionAppearance)
