from jinja2 import Template

GATHER_TMPL = Template(
    """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {% if play_url %}<Play>{{ play_url }}</Play>{% endif %}
  <Gather input="speech" action="{{ action }}" method="POST" speechTimeout="auto">
    <Say>Please tell me what you need.</Say>
  </Gather>
</Response>"""
)

PLAY_TMPL = Template(
    """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {% if play_url %}<Play>{{ play_url }}</Play>{% endif %}
  {% if redirect %}<Redirect method="POST">{{ redirect }}</Redirect>{% endif %}
</Response>"""
)

CONFIRM_TMPL = Template(
    """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>{{ text }}</Say>
  <Hangup/>
</Response>"""
)