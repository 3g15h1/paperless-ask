# Home Assistant: "coming up" notifications

Paperless Ask's `/api/upcoming` lists due and expiry dates from your documents. Its `alerts` list holds the
items that are exactly `ALERT_DAYS` days away today (default 14, 3, 1 and 0). Home Assistant can turn that
into a dashboard list and phone notifications.

Prefer a calendar? Subscribe to `http://<paperless-ask>/calendar.ics` in Apple Calendar, Google Calendar
or Outlook instead (on iPhone: open the "Add to calendar" link on the start page). Calendar alarms
fire at 09:00 on each alert day. Home Assistant isn't needed for that.

## 1. A sensor (configuration.yaml)

```yaml
rest:
  - resource: http://paperless-ask.local:8000/api/upcoming?days=60
    scan_interval: 1800
    # If you set ASK_PASSWORD:
    # authentication: basic
    # username: ask
    # password: !secret paperless_ask_password
    sensor:
      - name: "Paperless coming up"
        unique_id: paperless_ask_coming_up
        icon: mdi:calendar-alert
        value_template: "{{ value_json['items'] | count }}"
        json_attributes:
          - items
          - alerts
```

Use `value_json['items']`, not `value_json.items`. In templates, `.items` is the dictionary's built-in method.

Restart Home Assistant (or reload "REST entities"), and `sensor.paperless_coming_up` shows the number of
upcoming dates.

## 2. A morning notification (automation)

```yaml
alias: "Paperless: coming up"
triggers:
  - trigger: time
    at: "09:00:00"
conditions:
  - condition: template
    value_template: "{{ (state_attr('sensor.paperless_coming_up', 'alerts') or []) | count > 0 }}"
actions:
  - action: notify.mobile_app_your_phone   # your phone's notify service
    data:
      title: "Coming up"
      message: >-
        {% for a in state_attr('sensor.paperless_coming_up', 'alerts') %}{{ a.text }}
        {% endfor %}
      data:
        url: http://paperless-ask.local:8000/   # tapping opens Paperless Ask (iOS)
        clickAction: http://paperless-ask.local:8000/   # same, Android
```

Each alert's `text` is ready to show, e.g. "Example Mutual insurance expires in 14 days (Thu Oct 22)" or
"AcmePower: $142.37 due tomorrow (Fri Sep 5)".

## 3. A dashboard card

```yaml
type: markdown
title: Coming up
content: >-
  {% for i in state_attr('sensor.paperless_coming_up', 'items') or [] %}
  - **{{ 'Today' if i.days_left == 0 else i.days_left ~ ' d' }}** ·
  [{{ i.vendor }} {{ 'due' if i.label == 'due' else 'expires' }}]({{ i.url }})
  {{- ' · $%.2f' % i.amount if i.amount else '' }}
  {% else %}
  Nothing due in the next 60 days.
  {% endfor %}
```

## Item fields

| Field | Example |
|---|---|
| `id`, `title`, `url` | Paperless document id, title and link |
| `label` | `due` or `expires` |
| `date`, `days_left` | `2026-10-22`, `12` |
| `vendor` | Company from the fact card, else the Paperless correspondent or the title |
| `amount` | Amount due (due dates only, when known), else `null` |
| `text` | A ready-made sentence for notifications |

A due date is left out when the document has one of the `DONE_TAGS` (default `paid`). An expiry date is left
out once a newer document from the same company has a later expiry date, i.e. you've filed the renewal.
