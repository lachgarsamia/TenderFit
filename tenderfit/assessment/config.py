from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineConfig:
    name: str
    rules: bool = True
    guard: bool = True
    hybrid: bool = True
    coverage: bool = True
    agent: bool = False
    targeted: bool = True


BASELINES = {
    "B1": PipelineConfig("B1", rules=False, guard=False, hybrid=False, coverage=False),
    "B2": PipelineConfig("B2"),
    "B3": PipelineConfig("B3", agent=True),
    "B4": PipelineConfig("B4", agent=True, targeted=True),
    "B4-full": PipelineConfig("B4-full", agent=True, targeted=False),
}
