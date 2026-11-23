# -*- coding: utf-8 -*-
"""事件时间线技能（第五波，专用，服务 monitor 角色）：按主题检索公开来源，逐页抽取
"有来源记录的事件"，按时间排成一条线。

只导出 TimelineSkills / event_timeline / render_report / load_rules（工具层经
services["timeline_skills"] 取用）。每条事件必须带逐字锚定引文 + 来源 URL，
缺一丢弃记 limits；无日期事件进 undated 区不混主线；不判真假、不外推趋势、
不给分数、不建档。
"""
from .pipeline import TimelineSkills, event_timeline, load_rules, render_report

__all__ = ["TimelineSkills", "event_timeline", "render_report", "load_rules"]
