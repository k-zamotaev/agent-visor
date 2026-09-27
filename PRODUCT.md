# AgentVisor

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The operator delegates software tasks to a local model, follows the work over long runs,
adds requirements, and inspects evidence before accepting completion.

## Product Purpose

Complete a task through bounded working sessions, preserved state, independent milestone
review, and recovery from observable failures. Activity alone is not useful progress.

## Operating Context

The existing product uses FastAPI and plain JavaScript modules. It runs locally on Windows
and can expose an authenticated web panel. One task uses the model at a time. Russian and
English interfaces are supported. The operator can pause, resume, stop, and adjust limits.

## Capabilities and Constraints

The canonical plan, review receipts, and user instructions live in persistent task state.
Messages must retain their original text and distinguish delivery from actual execution.
Sending a message must not silently resume a paused task. Historical tool output and model
reasoning are observations, not proof of acceptance. Existing tasks and data survive upgrades.

## Brand Commitments

Preserve the AgentVisor name and logo. The user selected a familiar Codex/Claude workflow:
task list on the left, conversation in the center, plan and results on the right.

## Product Principles

- Keep the current task and the next available action clear.
- Put communication and useful results ahead of hardware telemetry.
- Preserve drafts, reading position, task state, and explicit user control.
- Show the difference between received, planned, and independently verified requirements.
