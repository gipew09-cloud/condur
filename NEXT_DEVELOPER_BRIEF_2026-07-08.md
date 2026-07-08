# Condur: next developer brief, GPS/statistics/routes

Updated: 2026-07-08

This is a focused handoff for the next developer. It records the latest owner feedback, screenshots, PDF evidence and product decisions. Do not treat this as a request to rewrite the whole product. Implement in small safe blocks, test each block, and update `HANDOFF.md`.

## Context: how the company actually works

This is not a classic dispatcher-created-order workflow yet.

Current real workflow:

- Logistics/customer people call the driver directly.
- The driver often already has keys and leaves by himself.
- There may be no internal dispatcher assigning a job inside Condur.
- Condur must therefore be a control system first: driver says what he is doing, GPS confirms or disproves it, owner sees money/documents/problems.

Future workflow:

- Later, dispatchers/logisticians can be added to Condur.
- Later, a customer order/request can become a formal entity.
- For now, do not force a heavy "client order" process on drivers.

Driver-facing wording should stay simple:

- Driver bot: "рейс" can be explained as "ходка".
- Driver chooses where he is going: warehouse/from and RC/to.
- Website can use "рейс / заявка", but do not make driver fill complex logistics data.

## Latest owner request

Owner wants these items recorded for implementation:

1. Consolidate trip timeline:
   `создан -> смена открыта -> выехал -> приехал на РЦ -> простой -> выехал с РЦ -> завершен -> выручка ждет подтверждения -> закрыт`.
2. Allow owner/admin to delete or correct a false statistics/geofence parking entry.
3. Investigate mismatch where site downtime differs from Stavtrack/PDF report.
4. Add planned route selection in driver bot from existing site directions/RC list.
5. GPS should confirm that the selected route was actually followed.
6. Do not auto-create complex customer orders yet.
7. Keep downtime as "potential to invoice", not automatic income.
8. Keep research from external TMS projects in mind, especially event timelines and audit logs.

## Evidence: suspicious downtime mismatch

Attached PDF reviewed:

- File: `/Users/ila/Downloads/У 774 УЕ 178_2026-07-04T21_00_00.000Z_2026-07-07T20_59_00.000Z.pdf`
- Report title: Stavtrack "История перемещений".
- Report period shown in PDF text: `05.07.2026 00:00` to `07.07.2026 23:59`.
- Object: `У 774 УЕ 178`.
- Total movement in report: `43.67 км`.

Important extracted PDF facts:

- On `05.07.2026`, movement group ends at `11:11:19`.
- Subtrip `1.6`: `10:47:15` from `7 с1 1-й Бадаевский проезд, Шушары` to `11:11:19` at `66 к1 литЗ улица Бабушкина...`.
- On `06.07.2026`, next movement group starts at `12:44:03` from `5А Шушары`.

Screenshot from site statistics shows a row:

- Vehicle: `У774ЕТ178`.
- RC: `РЦ 7 шагов, ... 1-й Бадаевский проезд, д.7`.
- Arrived: `05.07 11:11`.
- Left: `06.07 15:15`.
- Waited: `28 ч 3 мин`.
- Engine off: `9 ч 26 мин`.
- Potential invoice: `16 000 ₽`, `2 блока по 12 часов`.

This row is suspicious:

- The same `05.07 11:11` timestamp appears in Stavtrack as the end of a movement away from `1-й Бадаевский`, not necessarily as a confirmed parking arrival at `РЦ 7 шагов`.
- The site may have attached the parking interval to the wrong RC or wrong timestamp.
- Do not assume the site is correct. Investigate raw telemetry/events around this period.

What to inspect in DB/logs:

- `events` rows for `rc_arrived` and `rc_departed` for vehicle/stavtrack object `129772` / `У774УЕ178` around `2026-07-05 10:30` to `2026-07-06 16:30` Europe/Moscow.
- Payload fields: `vehicle_id`, `rc_id`, `parked_since`, `waited_minutes`, coordinates if present.
- `vehicle_telemetry_points` around `2026-07-05 11:11` and `2026-07-06 12:44-15:15`.
- Timezone handling: Stavtrack report and website must be compared in Moscow time.
- Whether `parked_long_enough` uses an old `motion_since_at` from the wrong point.
- Whether nearest-RC selection picked `РЦ 7 шагов` while the actual final position was another address.

## Required feature: delete/correct false RC parking entry

Owner needs a practical way to remove a false parking row from Statistics.

Do not physically erase raw GPS data. Preferred behavior:

- In `/stats` journal, add actions per parking row:
  - "Изменить"
  - "Скрыть / удалить"
- On delete, ask for confirmation and reason.
- Exclude hidden/deleted parking from:
  - KPI totals
  - RC summary
  - driver summary
  - potential invoice total
  - exports
- Keep audit trail: who hid it, when, why.
- Add optional filter later: "show hidden/deleted".

Implementation options:

Option A, quick:

- Add `ignored` / `ignored_reason` / `ignored_by_user_id` / `ignored_at` into `events.payload` for `rc_departed`.
- Statistics ignores `rc_departed` events with ignored flag.

Option B, cleaner:

- Add table `rc_downtime_adjustments` or `rc_downtimes`.
- Store a normalized downtime row derived from events.
- Add fields: `status=active|ignored|corrected`, corrected times, corrected RC, reason, actor.
- Statistics reads normalized rows.

Recommended now: Option B if implementation time is acceptable. It will be easier for Excel exports and future billing.

## Required feature: route selection in driver bot

Owner wants driver to choose a planned route from existing site data.

Meaning:

- In website, directions/routes and RCs already exist.
- In driver bot, when creating a trip, driver should choose:
  - warehouse/from
  - destination RC/to
  - or a saved route/direction if available
- This becomes the planned route of the trip.
- When GPS geofence detects arrival at an RC, Condur compares actual RC with planned destination.

Expected behavior:

- If actual RC matches planned RC:
  - Trip timeline says: `GPS подтвердил прибытие на РЦ ...`.
  - This strengthens trust that driver really completed the selected route.
- If actual RC differs:
  - Owner sees alert: `План: РЦ А, факт: РЦ Б`.
  - Do not automatically change route without owner confirmation.
- If driver chose "другой маршрут":
  - Ask simple text fields: from warehouse and destination RC/address.
  - Later owner can convert it into saved direction.

Important:

- Do not block driver with complex forms.
- Driver should be able to start quickly.
- The route selection is for confirmation/control, not for bureaucratic dispatch.

Suggested data model additions:

- `trips.planned_origin_rc_id` nullable.
- `trips.planned_destination_rc_id` nullable.
- `trips.route_id` or `direction_id` nullable if existing model supports it.
- `trips.actual_arrival_rc_id` nullable.
- `trips.actual_arrival_confirmed_at` nullable.
- Audit event: `trip.route_selected`.
- Audit event: `trip.actual_rc_confirmed`.
- Audit event: `trip.actual_rc_mismatch`.

## Trip timeline target

The trip page should become the central truth screen. Target sequence:

1. `Рейс создан`.
2. `Смена открыта`.
3. `Водитель выехал`.
4. `GPS: машина поехала`.
5. `GPS: приехал на РЦ`.
6. `Простой на РЦ начался`.
7. `Простой превысил норму`.
8. `Потенциально к выставлению: 8 000 / 16 000 / ...`.
9. `GPS: выехал с РЦ`.
10. `Водитель завершил рейс`.
11. `Выручка указана водителем`.
12. `Выручка ждет подтверждения владельца`.
13. `Выручка подтверждена владельцем`.
14. `Документы приложены / не приложены`.
15. `Рейс закрыт`.

This is partly implemented already, but must be unified. Use event/audit log where possible.

## Downtime billing rule

Business decision:

- Downtime must not be added to income automatically.
- It must be shown as "potential to invoice".
- 12 hours = 8,000 rub.
- Every full next 12 hours adds another 8,000 rub.
- 24 hours = 16,000 rub.
- 36 hours = 24,000 rub.

Expected examples:

- `11 ч 59 мин` -> `0 ₽`.
- `12 ч 00 мин` -> `8 000 ₽`.
- `14 ч 20 мин` -> `8 000 ₽`.
- `28 ч 03 мин` -> `16 000 ₽`, label: `2 блока по 12 часов`.

UI requirement:

- Show this clearly in red/orange.
- On `/stats`, row with billable downtime must be visually obvious.
- On vehicle/trip context, show related RC downtime if linked.
- Text should say "потенциально к выставлению", not "доход".

## Bug/design note: expenses delete button

Screenshot `/expenses` shows the delete action as a large blue bar in the table row. This is confusing.

Fix later:

- Make delete a small red text/icon button.
- Add confirmation.
- Keep edit as normal link.
- Do not make destructive action look like a primary blue submit button.

## External project note: fossabot/open-tms

Owner mentioned `https://github.com/fossabot/open-tms`.

Previous research conclusion: low practical value compared with:

- `loadpartner/tms`
- `dominicfinn/open_tms`
- `fleetbase`
- `frappe/erpnext`
- `nelsonmpanju/Fleet-Management-System`

Do not spend time copying architecture from `fossabot/open-tms` unless a later review finds a specific useful module.

## Tests to add when implementing

Add focused tests:

- `billable_downtime_amount(719 min) == 0`.
- `billable_downtime_amount(720 min) == 8000`.
- `billable_downtime_amount(860 min) == 8000`.
- `billable_downtime_amount(1683 min) == 16000`.
- Hidden/deleted downtime row is excluded from stats totals.
- Corrected downtime row recalculates amount.
- Route selected in bot is stored on trip.
- Matching actual RC creates confirmation event.
- Different actual RC creates mismatch event.
- Trip timeline renders planned route, actual RC and revenue pending/confirmed states.

## Suggested priority order

1. Investigate the concrete U774UE178 downtime mismatch.
2. Add owner/admin "hide/delete downtime row" with audit.
3. Add downtime correction if needed.
4. Add downtime billing helper and tests for 12-hour blocks.
5. Unify trip timeline states.
6. Add route selection in driver bot from existing directions/RC data.
7. Add GPS confirmation/mismatch between planned and actual RC.
8. Clean up expenses delete button design.

