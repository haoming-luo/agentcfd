"""Dependency-free rendering for compact campaign decision surfaces."""

from __future__ import annotations

import html
from typing import Mapping, Sequence


def _number(value: float) -> str:
    magnitude = abs(value)
    if magnitude != 0.0 and (magnitude >= 1.0e4 or magnitude < 1.0e-3):
        return f"{value:.3e}"
    return f"{value:.6g}"


def _limits(values: Sequence[float]) -> tuple[float, float]:
    lower = min(values)
    upper = max(values)
    if lower == upper:
        padding = max(abs(lower) * 0.05, 1.0)
    else:
        padding = (upper - lower) * 0.05
    return lower - padding, upper + padding


def render_operating_map_svg(
    report: Mapping[str, object],
    *,
    title: str,
) -> str:
    """Render one validated campaign operating-map record as accessible SVG."""

    points = report["points"]
    x_values = [float(point["x"]) for point in points]
    y_values = [float(point["y"]) for point in points]
    x_min, x_max = _limits(x_values)
    y_min, y_max = _limits(y_values)
    width, height = 960, 600
    left, right, top, bottom = 112, 42, 72, 92
    plot_width = width - left - right
    plot_height = height - top - bottom

    def project_x(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * plot_width

    def project_y(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * plot_height

    x_axis = report["x_axis"]
    y_axis = report["y_axis"]
    x_label = str(x_axis["label"])
    y_label = str(y_axis["label"])
    if x_axis["unit"]:
        x_label += f" [{'-' if x_axis['unit'] == '1' else x_axis['unit']}]"
    if y_axis["unit"]:
        y_label += f" [{'-' if y_axis['unit'] == '1' else y_axis['unit']}]"
    svg: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
            f'aria-labelledby="title description">'
        ),
        f'<title id="title">{html.escape(title)}</title>',
        (
            '<desc id="description">AgentCFD campaign operating map using compact '
            'accepted result quantities; volumetric fields were not opened.</desc>'
        ),
        '<rect width="100%" height="100%" fill="#f7f8fa"/>',
        f'<text x="{left}" y="38" font-family="system-ui, sans-serif" '
        f'font-size="24" font-weight="650" fill="#16212b">{html.escape(title)}</text>',
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" '
        'fill="#ffffff" stroke="#ccd3da"/>',
    ]
    for index in range(6):
        fraction = index / 5
        x_value = x_min + fraction * (x_max - x_min)
        x_position = left + fraction * plot_width
        y_value = y_max - fraction * (y_max - y_min)
        y_position = top + fraction * plot_height
        svg.extend(
            [
                f'<line x1="{x_position:.3f}" y1="{top}" x2="{x_position:.3f}" '
                f'y2="{top + plot_height}" stroke="#e6eaee"/>',
                f'<text x="{x_position:.3f}" y="{top + plot_height + 28}" '
                'text-anchor="middle" font-family="ui-monospace, monospace" '
                f'font-size="12" fill="#4f5d69">{html.escape(_number(x_value))}</text>',
                f'<line x1="{left}" y1="{y_position:.3f}" x2="{left + plot_width}" '
                f'y2="{y_position:.3f}" stroke="#e6eaee"/>',
                f'<text x="{left - 14}" y="{y_position + 4:.3f}" text-anchor="end" '
                'font-family="ui-monospace, monospace" font-size="12" '
                f'fill="#4f5d69">{html.escape(_number(y_value))}</text>',
            ]
        )
    svg.extend(
        [
            f'<text x="{left + plot_width / 2:.3f}" y="{height - 28}" '
            'text-anchor="middle" font-family="system-ui, sans-serif" '
            f'font-size="15" fill="#263541">{html.escape(x_label)}</text>',
            f'<text x="28" y="{top + plot_height / 2:.3f}" '
            f'transform="rotate(-90 28 {top + plot_height / 2:.3f})" '
            'text-anchor="middle" font-family="system-ui, sans-serif" '
            f'font-size="15" fill="#263541">{html.escape(y_label)}</text>',
        ]
    )
    accepted_points = [point for point in points if point["accepted"]]
    if report["connected_accepted_curve"]:
        coordinates = " ".join(
            f'{project_x(float(point["x"])):.3f},{project_y(float(point["y"])):.3f}'
            for point in accepted_points
        )
        svg.append(
            f'<polyline points="{coordinates}" fill="none" stroke="#087f5b" '
            'stroke-width="3" stroke-linejoin="round"/>'
        )
    for point in points:
        x_position = project_x(float(point["x"]))
        y_position = project_y(float(point["y"]))
        accepted = bool(point["accepted"])
        color = "#087f5b" if accepted else "#d97706"
        label = point["design_point_name"] or point["run_id"]
        tooltip = (
            f"{label}: {x_axis['name']}={_number(float(point['x']))}, "
            f"{y_axis['name']}={_number(float(point['y']))}, "
            f"accepted={str(accepted).lower()}"
        )
        svg.append(
            f'<circle cx="{x_position:.3f}" cy="{y_position:.3f}" r="6" '
            f'fill="{color if accepted else "#ffffff"}" stroke="{color}" '
            f'stroke-width="3"><title>{html.escape(tooltip)}</title></circle>'
        )
    svg.extend(
        [
            f'<circle cx="{left}" cy="{height - 58}" r="5" fill="#087f5b"/>',
            f'<text x="{left + 12}" y="{height - 54}" font-family="system-ui, sans-serif" '
            'font-size="12" fill="#4f5d69">accepted result</text>',
        ]
    )
    if any(not point["accepted"] for point in points):
        svg.extend(
            [
                f'<circle cx="{left + 132}" cy="{height - 58}" r="5" fill="#ffffff" '
                'stroke="#d97706" stroke-width="2"/>',
                f'<text x="{left + 144}" y="{height - 54}" '
                'font-family="system-ui, sans-serif" font-size="12" '
                'fill="#4f5d69">unaccepted evidence</text>',
            ]
        )
    svg.extend(
        [
            f'<text x="{left + plot_width}" y="{height - 54}" text-anchor="end" '
            'font-family="system-ui, sans-serif" font-size="11" fill="#66737e">'
            f'{report["point_count"]} plotted / {len(report["exclusions"])} excluded; no field payload read</text>',
            '</svg>',
        ]
    )
    return "\n".join(svg) + "\n"


__all__ = ["render_operating_map_svg"]
