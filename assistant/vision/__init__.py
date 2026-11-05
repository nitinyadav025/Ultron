"""Real-time screen eyes and hands for GNOME Wayland.

``atspi_helper`` is a tiny stdlib+gi program that must run under the system Python
(``/usr/bin/python3``) because the project venv does not link GTK/AT-SPI. The rest of the
package runs in the project venv and talks to it over JSON lines.
"""
