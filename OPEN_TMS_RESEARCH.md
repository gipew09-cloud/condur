# Condur: TMS, Fleet and ERP research map

Updated: 2026-07-08

This note summarizes external TMS/fleet/ERP projects reviewed as product and architecture references for Condur. The goal is not to copy another system, but to build a practical low-budget logistics control product around Telegram, web cabinet, Stavtrack GPS and owner approval workflows.

## Main Conclusion

Condur should stay lightweight and operational: Telegram for drivers, web cabinet for owners/admins, Stavtrack for vehicle facts, and Postgres as the source of truth. Do not turn Condur into a heavy ERP now.

The best direction is to borrow ideas in layers:

- `loadpartner/tms`: shipment lifecycle, stops, appointments, receivables/payables, audit trail.
- `dominicfinn/open_tms`: event-driven architecture, immutable event log, read models, webhooks, integrations, SLA/anomaly logic.
- `fleetbase`: modular logistics platform vision, dispatch, portals, API-first product shape.
- `frappe/erpnext`: delivery trip/stops, vehicle/driver/accounting/document patterns.
- `nelsonmpanju/Fleet-Management-System`: fuel requests, trip funds, breakdowns, inspections, settlement workflow.
- `neozhu/tms`: tariffs, freight/accounting forms, Excel-like operational paperwork.
- `frappe/frappe_docker`: deployment reference for complex Frappe/ERPNext stacks, not a feature source.

Avoid copying code directly, especially from GPL projects such as ERPNext/Frappe-based apps. Use concepts and reimplement them in Condur's own codebase.

## New Projects Reviewed

### dominicfinn/open_tms

Open-source TypeScript TMS with React, Fastify, Prisma, PostgreSQL and workers. It is more of a modern architecture reference than a ready product for our exact use.

Useful ideas:

- Domain events for everything important: shipment created, status changed, document uploaded, telemetry received, issue opened.
- Immutable event log as the audit base.
- Separate read models for dashboards so heavy pages do not recalculate everything live.
- Shipment lifecycle with timeline and transitions.
- IoT/device telemetry endpoints and webhooks.
- Customer/carrier portal ideas for later.
- SLA and issue/anomaly handling.

What to take into Condur:

- Add a universal event journal: `trip.created`, `trip.started`, `trip.completed`, `shift.started`, `shift.closed`, `revenue.pending`, `revenue.confirmed`, `rc.entered`, `rc.left`, `telemetry.received`, `gps.stale`, `trip.deleted`, `shift.deleted`.
- Build dashboards from events and summary tables, not from fragile scattered logic.
- Keep every owner/admin action auditable.

What not to take now:

- Full CQRS complexity everywhere.
- Large customer/carrier portals before core operations are stable.

### neozhu/tms

Older .NET/Entity Framework TMS. It is useful as a reference for traditional transport paperwork and tariff/accounting logic.

Useful ideas:

- Freight/tariff rules.
- Waybill/order style screens.
- Excel import/export mindset.
- Structured dictionaries for customers, locations, vehicles and drivers.

What to take into Condur:

- Route/RC/customer tariff cards.
- Exportable tables for finance, downtime, trips and acts.
- Better reference data instead of free text only.

### loadpartner/tms

One of the strongest references for Condur. Modern Laravel/React TMS with shipment states, stops, appointments, payables/receivables, documents, notes and audit.

Useful ideas:

- Shipment statuses: pending, booked, dispatched, at pickup, in transit, at delivery, delivered, cancelled.
- Stops and appointments as first-class objects.
- Separate customer revenue and carrier/driver costs.
- Documents and notes attached to each shipment.
- Audit trail for operational changes.

What to take into Condur:

- Stronger trip lifecycle: created, shift opened, departed, arrived RC, waiting, unloaded, completed, revenue pending, revenue confirmed.
- Timeline on trip page with all facts: driver actions, GPS events, RC waiting, documents, revenue, owner approvals.
- Delete and restore for trips/shifts with reason and audit log.
- Finance separation: revenue, fuel, other expenses, potential downtime invoice.

### Fleetbase

Fleetbase is an open-source modular logistics/delivery platform. It is useful as a product vision reference: platform, portals, dispatch, real-time tracking, routing and API.

Useful ideas:

- Modular product structure.
- Fleet and driver management.
- Order and dispatch management.
- Real-time tracking and routing.
- Customer and merchant portals.
- API-first approach.

What to take into Condur:

- Keep modules clean: trips, shifts, vehicles, drivers, RC/routes, map, finance, documents, settings.
- Later: customer portal where a customer can see only their deliveries/documents.
- Later: public tracking link for a single trip.
- Later: internal API/webhooks for integrations.

What not to take now:

- Marketplace/large platform complexity.
- Too many roles before owner/admin/driver flow is polished.

### frappe/erpnext

ERPNext is a full ERP. The relevant reference is Delivery Trip and Delivery Stop logic.

Useful ideas:

- Delivery Trip validates driver, customer contacts, stops and distances.
- Stops can contain customer/address/contact/ETA/distance/status.
- Status changes trigger linked document updates.
- Customers can be notified about driver, vehicle and estimated arrival.
- Strong accounting/document model.

What to take into Condur:

- Trip stops should become more structured over time: pickup, RC/unloading, optional return/parking.
- Add ETA/planned arrival fields later.
- Link trip facts to documents and finance.
- Generate acts of completed work and downtime appendices from structured trip data.

What not to take now:

- Full ERP accounting, stock and CRM.
- Migration to Frappe/ERPNext as the main platform.

### frappe/frappe_docker

This is infrastructure for running Frappe/ERPNext, not a TMS product.

Useful ideas:

- Clear multi-service deployment structure.
- Worker/web/socket/scheduler separation.
- Environment-based deployment discipline.

What to take into Condur:

- Keep Railway services separated by role: main web/bots/scheduler and EGTS receiver.
- Keep service roles explicit.
- In the future, split long-running background jobs if load grows.

### nelsonmpanju/Fleet-Management-System

Frappe/ERPNext fleet app. Strong operational reference for transport companies with fuel, funds, breakdowns, assignments and trip completion.

Useful ideas:

- Cargo registration to vehicle assignment to trip execution to financial settlement.
- Transport assignment before trip.
- Fuel request workflow.
- Trip funds/requested payment workflow.
- Breakdown management.
- Truck and trailer inspections.
- Driver/vehicle/route tables.
- Audit trail and role-based access.

What to take into Condur:

- Driver requests fuel/cash from bot, owner approves or rejects.
- Breakdown button in driver bot: photo, location, text, status.
- Pre-trip or weekly vehicle inspection checklist.
- Trip settlement page: revenue, fuel, other expenses, documents, downtime, owner confirmation.
- Route/RC dictionary with standard waiting rules.

What not to take now:

- Full ERPNext dependency.
- Complex purchase orders/journal entries until finance process is clearer.

### ERPNext GitHub Topic

The GitHub topic is useful for discovery only. It is not a single product. Treat it as a place to find examples of Frappe/ERPNext apps, but do not base Condur on topic search alone.

## Previously Reviewed Projects

### openwms/org.openwms.tms.transportation

Warehouse/transportation management reference. Useful mainly for strict state machines and controlled transport order status.

Take:

- Clear status transitions.
- Separation between planned order and executed movement.

Do not take:

- Warehouse-heavy architecture.

### titastransport/Tms-software

Simple PHP/MySQL transport admin panel. Useful only as a reminder that simple CRUD screens matter.

Take:

- Fast add/edit/list flows.

Do not take:

- Architecture or UI patterns as a main reference.

### kongko/OpenTMS, ThePhalcons/FalconTMS, fossabot/open-tms

Low practical value for Condur compared with loadpartner, Fleetbase, ERPNext and VSD Fleet.

## What This Means For Condur

### Product Direction

Condur is not a professional enterprise TMS yet. Its advantage should be practical owner control:

- What driver says in Telegram.
- What car actually does by GPS.
- Where money was entered.
- Where documents are missing.
- Where downtime is billable.
- Where a driver, shift or vehicle behavior looks suspicious.

### Core Model To Build Around

Use these entities as the long-term backbone:

- Company/account.
- User/admin/owner/driver.
- Vehicle.
- Driver.
- Shift.
- Trip.
- Trip event/timeline.
- RC/warehouse/geofence.
- Route/direction.
- Expense.
- Fuel request.
- Revenue approval.
- Document.
- Downtime event.
- Audit/event log.

### Trip Timeline Should Become Central

Each trip page should show one clear timeline:

1. Trip created.
2. Driver assigned.
3. Vehicle assigned.
4. Shift opened.
5. Driver departed.
6. GPS confirms movement.
7. Arrived at RC/geofence.
8. Waiting started.
9. Waiting crossed norm.
10. Potential downtime invoice calculated.
11. Left RC.
12. Trip completed.
13. Revenue entered.
14. Revenue confirmed by owner.
15. Documents uploaded.
16. GPS vs odometer checked.

### Downtime Billing Rule

Current business rule:

- Downtime is not added to income automatically.
- It is shown as "potential to invoice".
- 12 hours = 8,000 rub.
- Every next full 12 hours adds another 8,000 rub.
- 24 hours = 16,000 rub.
- 36 hours = 24,000 rub.

The UI should highlight this in red/orange:

- On statistics.
- On RC/routes.
- On vehicle card.
- On trip timeline if related to trip.
- On operational control panel.

### Deleting Trips And Shifts

Needed feature:

- Owner/admin can delete a trip.
- Owner/admin can delete a shift.
- Deletion must ask for confirmation and reason.
- Prefer soft delete first, not physical delete.
- Deleted trip/shift should disappear from normal lists but remain in audit.
- Add restore later.

This prevents accidental data loss and protects from human mistakes.

### Fuel, Cash And Breakdown Workflow

Next useful workflow from VSD Fleet:

- Driver presses "Need fuel" or "Need cash".
- Enters amount/reason/photo.
- Owner gets approve/reject buttons.
- If approved, it attaches to trip/shift/vehicle.
- It appears in finance and driver settlement.

Breakdown workflow:

- Driver presses "Breakdown".
- Bot asks photo, comment, location.
- Owner sees alert.
- Vehicle gets "problem" status until resolved.

### Statistics Improvements

Statistics should be an operational control center, not just tables.

Add sections:

- Problems now: GPS silent, no shift, standing at RC, movement without trip, stale documents, unconfirmed revenue.
- RC performance: arrivals, average waiting, over-norm waiting, potential invoice.
- Driver quality: trips, revenue, missing docs, forgotten shifts, unconfirmed revenue, GPS/odometer mismatch.
- Vehicle quality: GPS health, mileage, downtime, documents, active issues.
- Route profitability: revenue, costs, margin, average time, RC waiting.
- Weekly rhythm: trips/revenue/downtime by week.

### Design Direction

Use the new design mockups as direction:

- Dense operational dashboard.
- Strong top navigation.
- Cards for repeated entities only.
- More useful status colors.
- Timeline on trips.
- Red/orange highlights for money risk and urgent problems.
- Tables remain readable on desktop.
- Mobile needs compact bottom navigation and no hidden exit/settings buttons.

Do not remove existing features when restyling.

### Architecture Direction

Recommended next architecture step:

- Add an event/audit table.
- Write important actions into it.
- Use it for timeline, audit, debugging and future dashboards.
- Keep the existing monolith for now.
- Do not split into many services until real load appears.

Later:

- Background worker for heavy recalculations.
- Webhooks/API for customers or integrations.
- Public tracking pages.
- Customer portal.

## Priority Backlog

### Priority 1

- Fix/finish trip page timeline in the new design style.
- Add delete trip and delete shift with reason and audit.
- Make downtime slab calculation: 12h = 8,000, 24h = 16,000, etc.
- Show potential downtime invoice in statistics, route/RC, vehicle and trip context.
- Improve map and vehicle cards so ignition/GPS uncertainty is explained clearly.
- Add event/audit log foundation.
- Make small-screen navigation usable.

### Priority 2

- Fuel/cash approval workflow.
- Breakdown workflow.
- Better route and RC dictionaries.
- Acts of completed work.
- Downtime appendix/export to Excel.
- Driver quality page.
- Vehicle quality page.

### Priority 3

- Customer portal.
- Public tracking link.
- API/webhooks.
- Advanced SLA/anomaly engine.
- More formal accounting integration.

