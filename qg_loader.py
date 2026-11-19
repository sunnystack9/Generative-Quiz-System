"""
Helper for Quiz-gram.
Put this file in the ROOT folder, next to app.py (NOT inside pages/).

Streamlit's declare_component() needs to be called from a normal importable
module. Scripts inside pages/ have no module, which causes:
    RuntimeError: module is None. This should never happen.
"""

import streamlit.components.v1 as components


def make_component(path):
    return components.declare_component("quizgram", path=str(path))
