"""Thread-safe, off-event-loop grade distribution chart renderer with accessible text alternatives.

Uses Matplotlib's object-oriented ``Figure`` + ``FigureCanvasAgg`` API directly instead of
``matplotlib.pyplot`` or ``plt.style.use(...)`` so rendering is completely thread-safe,
never mutates process-global rcParams, closes resources deterministically, and stays within
Discord attachment byte bounds.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

LETTER_GRADES: tuple[str, ...] = (
    "F-S",
    "F",
    "FNA",
    "D-",
    "D",
    "D+",
    "C-",
    "C",
    "C+",
    "B-",
    "B",
    "B+",
    "A-",
    "A",
    "A+",
)

MAX_CHART_PROFESSORS = 8
MAX_CHART_BYTES = 2 * 1024 * 1024  # 2 MiB (well under Discord's 8/10 MiB attachment limit)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# High-contrast accessible palette legible on dark Discord surfaces
_BAR_PALETTE: tuple[str, ...] = (
    "#66C2A5",
    "#FC8D62",
    "#8DA0CB",
    "#E78AC3",
    "#A6D854",
    "#FFD92F",
    "#E5C494",
    "#B3B3B3",
)


class ChartRenderError(RuntimeError):
    """Raised when chart rendering fails or violates Discord attachment bounds."""


@dataclass(frozen=True)
class ChartRenderResult:
    """Validated PNG chart payload and accessible text summary."""

    png_bytes: bytes
    size_bytes: int
    plotted_count: int
    truncated_count: int
    accessible_alt_text: str

    def to_bytes_io(self) -> io.BytesIO:
        buf = io.BytesIO(self.png_bytes)
        buf.seek(0)
        return buf


def _bucket_percentages(dist: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Summarize raw letter-grade counts into coarse A/B/C/D/F percentage buckets."""
    buckets = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    total = 0
    for item in dist:
        grade = str(item.get("letterGrade") or "").strip().upper()
        try:
            count = max(0, int(item.get("count", 0)))
        except (TypeError, ValueError):
            count = 0
        total += count
        if grade.startswith("A"):
            buckets["A"] += count
        elif grade.startswith("B"):
            buckets["B"] += count
        elif grade.startswith("C"):
            buckets["C"] += count
        elif grade.startswith("D"):
            buckets["D"] += count
        elif grade.startswith("F"):
            buckets["F"] += count

    if total <= 0:
        return {}
    return {k: round((v / total) * 100.0, 1) for k, v in buckets.items() if v > 0}


def build_accessible_grade_summary(
    course_title: str,
    professors: Sequence[Mapping[str, Any]],
    *,
    max_items: int = MAX_CHART_PROFESSORS,
) -> str:
    """Generate a concise, screen-reader-friendly plain-text grade distribution summary."""
    if not professors:
        return f"Grade distribution chart for {course_title}: no instructor distribution data available."

    shown = list(professors[:max_items])
    lines: list[str] = [f"Grade distribution for {course_title} ({len(professors)} instructor(s)):"]
    for prof in shown:
        name = str(prof.get("name") or "Unknown").strip()
        students = str(prof.get("students") or "N/A").strip()
        avg = str(prof.get("avg") or "N/A").strip()
        common = str(prof.get("common") or "N/A").strip()
        buckets = _bucket_percentages(prof.get("dist") or ())
        bucket_str = (
            ", ".join(f"{k}: {pct:.0f}%" for k, pct in buckets.items()) if buckets else "no letter breakdown"
        )
        lines.append(f"- {name} ({students} students, {avg}, {common}): {bucket_str}")

    if len(professors) > max_items:
        lines.append(f"(+{len(professors) - max_items} additional instructor(s) omitted from chart)")
    return "\n".join(lines)[:950]


def render_grade_distribution_chart(
    course_title: str,
    professors: Sequence[Mapping[str, Any]],
    *,
    max_professors: int = MAX_CHART_PROFESSORS,
    max_bytes: int = MAX_CHART_BYTES,
    dpi: int = 100,
) -> ChartRenderResult:
    """Render a grouped bar chart via thread-safe Matplotlib OO API without global pyplot state."""
    plotted = list(professors[: max(1, int(max_professors))])
    truncated = max(0, len(professors) - len(plotted))
    alt_text = build_accessible_grade_summary(course_title, professors, max_items=max_professors)

    fig = Figure(figsize=(10, 6), dpi=dpi, facecolor="#1E1F22")
    canvas = FigureCanvasAgg(fig)
    try:
        ax = fig.add_subplot(111, facecolor="#2B2D31")
        x = np.arange(len(LETTER_GRADES))
        num_profs = len(plotted)

        if num_profs == 0:
            ax.text(
                0.5,
                0.5,
                "No Distribution Data",
                ha="center",
                va="center",
                color="#F2F3F5",
                fontsize=14,
                transform=ax.transAxes,
            )
            ax.set_xticks([])
            ax.set_yticks([])
        else:
            total_width = 0.8
            width = total_width / num_profs

            for i, prof in enumerate(plotted):
                dist = prof.get("dist") or ()
                counts_map: dict[str, int] = {}
                for item in dist:
                    grade = str(item.get("letterGrade") or "").strip()
                    try:
                        cnt = max(0, int(item.get("count", 0)))
                    except (TypeError, ValueError):
                        cnt = 0
                    if grade:
                        counts_map[grade] = counts_map.get(grade, 0) + cnt

                total_students = sum(counts_map.values())
                y_vals = [
                    (counts_map.get(letter, 0) / total_students * 100.0) if total_students > 0 else 0.0
                    for letter in LETTER_GRADES
                ]
                offset = (i - num_profs / 2) * width + width / 2
                label_name = str(prof.get("name") or f"Instructor {i + 1}")[:40]
                ax.bar(
                    x + offset,
                    y_vals,
                    width,
                    label=label_name,
                    color=_BAR_PALETTE[i % len(_BAR_PALETTE)],
                )

            ax.set_ylabel("Percentage of Students", color="#DBDEE1")
            title_suffix = f" (Top {num_profs} of {len(professors)})" if truncated > 0 else ""
            ax.set_title(
                f"{str(course_title)[:80]} - Grade Distribution{title_suffix}",
                color="#F2F3F5",
                pad=12,
            )
            ax.set_xticks(x)
            ax.set_xticklabels(LETTER_GRADES, color="#DBDEE1")
            ax.tick_params(axis="y", colors="#DBDEE1")
            ax.yaxis.grid(True, linestyle="--", alpha=0.3, color="#949BA4")
            ax.set_axisbelow(True)
            legend = ax.legend(facecolor="#1E1F22", edgecolor="#4E5058", labelcolor="#F2F3F5")
            if legend is not None:
                legend.get_frame().set_alpha(0.9)

        for spine in ax.spines.values():
            spine.set_color("#4E5058")

        buf = io.BytesIO()
        canvas.print_png(buf)
        png_bytes = buf.getvalue()
    finally:
        fig.clear(keep_observers=False)
        canvas.figure = None  # type: ignore[assignment]
        fig.canvas = None  # type: ignore[assignment]

    if not png_bytes.startswith(_PNG_MAGIC):
        raise ChartRenderError("Rendered chart output did not produce valid PNG bytes.")
    if len(png_bytes) > max_bytes:
        raise ChartRenderError(
            f"Rendered chart ({len(png_bytes)} bytes) exceeds Discord attachment bound ({max_bytes} bytes)."
        )

    return ChartRenderResult(
        png_bytes=png_bytes,
        size_bytes=len(png_bytes),
        plotted_count=len(plotted),
        truncated_count=truncated,
        accessible_alt_text=alt_text,
    )


async def render_grade_distribution_chart_async(
    course_title: str,
    professors: Sequence[Mapping[str, Any]],
    *,
    max_professors: int = MAX_CHART_PROFESSORS,
    max_bytes: int = MAX_CHART_BYTES,
    dpi: int = 100,
) -> ChartRenderResult:
    """Render grade distribution chart in a worker thread off the Discord asyncio event loop."""
    return await asyncio.to_thread(
        render_grade_distribution_chart,
        course_title,
        professors,
        max_professors=max_professors,
        max_bytes=max_bytes,
        dpi=dpi,
    )
