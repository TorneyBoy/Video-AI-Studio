"""Whole-frame authoring without stretching source windows or dropping clips."""
from dataclasses import dataclass
from fractions import Fraction
from functools import cached_property
import math


def _number(value: float) -> Fraction:
    if not math.isfinite(value):
        raise ValueError("时间与帧率必须为有限数值")
    return Fraction(str(value))


@dataclass(frozen=True)
class FrameClock:
    fps: float

    def __post_init__(self):
        if not math.isfinite(self.fps) or not 0 < self.fps <= 240:
            raise ValueError("帧率必须在0至240之间")

    @cached_property
    def rate(self) -> Fraction:
        return _number(self.fps).limit_denominator(1_000_000)

    def _scaled(self, seconds: float) -> Fraction:
        scaled = _number(seconds) * self.rate
        nearest = round(scaled)
        # Absorb binary float serialization noise at an existing frame edge,
        # never a meaningful fraction of a frame.
        return Fraction(nearest) if abs(scaled - nearest) <= Fraction(1, 100_000_000) else scaled

    def floor(self, seconds: float) -> int:
        value = self._scaled(seconds)
        return value.numerator // value.denominator

    def ceil(self, seconds: float) -> int:
        value = self._scaled(seconds)
        return -(-value.numerator // value.denominator)

    def seconds(self, frame: int) -> float:
        return float(Fraction(frame) / self.rate)

    def window(self, start: float, end: float) -> tuple[int, int]:
        if start < 0 or end <= start:
            raise ValueError("素材源区间无效")
        first, last = self.ceil(start), self.floor(end)
        if last <= first:
            raise ValueError("素材镜头不足当前帧率的一帧，不能隐式延长")
        return first, last


def allocate_frames(capacities: list[int], total: int) -> list[int]:
    if not capacities or any(value < 1 for value in capacities):
        raise ValueError("每个镜头都必须包含至少一个完整帧")
    if total < len(capacities):
        raise ValueError("目标时长不足以容纳每个入选镜头的一帧，请增加时长或减少镜头")
    if total > sum(capacities):
        raise ValueError("目标帧数超出可用素材")
    weight = sum(capacities)
    ideal = [Fraction(total * capacity, weight) for capacity in capacities]
    allocation = [max(1, min(capacity, value.numerator // value.denominator)) for capacity, value in zip(capacities, ideal)]
    while sum(allocation) > total:
        donor = max((index for index, value in enumerate(allocation) if value > 1),
                    key=lambda index: (allocation[index] - ideal[index], -index))
        allocation[donor] -= 1
    while sum(allocation) < total:
        recipient = max((index for index, value in enumerate(allocation) if value < capacities[index]),
                        key=lambda index: (ideal[index] - allocation[index], -index))
        allocation[recipient] += 1
    return allocation
