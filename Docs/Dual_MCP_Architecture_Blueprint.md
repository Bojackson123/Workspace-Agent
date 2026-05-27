# Architectural Blueprint: Dual-MCP Google Workspace Agent

## System Overview
This document outlines the enterprise-grade AI automation architecture for a Google Chat-based agent. The system leverages the Google Agent Development Kit and routes tool calls through two distinct Model Context Protocol (MCP) servers deployed on Google Cloud Run. This dual-server pattern establishes a strict security boundary between user context gathering and workflow execution.

## Core Components

### 1. The Interface & Agent Backend (FastAPI)
* **Entry Point:** Receives POST payloads directly from Google Chat.
* **Security Protocol (The Header):** Strictly verifies the incoming Google Chat JWT (`Authorization: Bearer <token>`) signature. Validates that the audience matches the Cloud Run URL and the issuer/email is exactly `chat@system.gserviceaccount.com` before processing the payload.
* **Identity Extraction (The Body):** Safely extracts the authenticated human user's email from the validated JSON payload (e.g., `payload["user"]["email"]`).
* **Agent Routing:** Passes the user's prompt and verified email to the core LLM logic (e.g., LangChain/Vertex AI). The agent determines which of the two MCP servers to invoke.

### 2. Server A: The Context MCP (Read-Only)
* **Deployment:** Google Cloud Run service.
* **Purpose:** Securely fetch personal context (Emails, Docs, Chat history) to ground the LLM's responses.
* **Authentication:** Utilizes **Domain-Wide Delegation (DWD)**.
* **Flow:** The FastAPI agent passes the extracted `user_email` to this server. The server impersonates that exact user.
* **Scope:** Strictly limited to `.readonly` API scopes (e.g., `https://www.googleapis.com/auth/gmail.readonly`).
* **Security Boundary:** If the agent hallucinates or suffers prompt injection, it physically lacks the scopes to modify or delete the user's private data.

### 3. Server B: The Action MCP (Read/Write)
* **Deployment:** Google Cloud Run service.
* **Purpose:** Execute business workflows, create documents, and manage data.
* **Authentication:** Utilizes **Application Default Credentials (ADC)** tied to the Cloud Run instance's Service Account identity. No user impersonation occurs here.
* **Scope:** Read/Write access (e.g., `drive`, `documents`, `spreadsheets`).
* **Strict Storage Invariant:** The service account's root drive is strictly off-limits. Files are never needed in, and must never be placed into, the hidden root drive. Every write operation MUST target an explicitly specified Shared Drive where the service account holds Contributor/Manager permissions.
* **Tooling Enforcement:** Tools like `create_workspace_file` are hardcoded to require a `parent_folder_id` pointing to the designated Shared Drive to prevent orphaned files.

## Authentication & Request Flow Summary
1. **Google Chat** sends an HTTP POST to the FastAPI backend.
2. **FastAPI Header Check:** Validates the cryptographic signature of the Bearer Token to prove Google sent it.
3. **FastAPI Body Parse:** Extracts the human user's email (`user.email`) from the JSON body.
4. **Agent Invocation:** FastAPI sends the prompt and the extracted email to the LLM agent.
5. **Context Gathering:** Agent calls Server A with the email. Server A impersonates the user to read context.
6. **Action Execution:** Agent calls Server B. Server B uses its own Service Account identity to write outputs to the Shared Drive.

## Coding Agent Instructions & Skills Matrix
When implementing the core agent logic, enforce the following behavioral rules:

1. **Identity Handling:** You will be provided a `user_email` with every prompt. Route all "search my email" or "find my document" tool calls to Server A using this email.
2. **State Management:** Do not assume document names or IDs. Always query Server B's `search_drive` tool to fetch exact file/folder IDs within the Shared Drive before attempting modifications.
3. **Data Isolation:** Never attempt to use Server B tools to manipulate data found via Server A. Data from Server A is strictly for reading context. All outputs generated from that context must be written via Server B into the designated Shared Drive.
4. **FastAPI Security:** Ensure the Google Chat JWT verification strictly checks the `aud` (Cloud Run URL) and the `email` claim (`chat@system.gserviceaccount.com`).
