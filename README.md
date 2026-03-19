# 🏥 AI-Powered Appointment Booking System

This project implements an intelligent appointment booking system for a clinic, combining conversational AI, event-driven architecture, and cloud integrations.

The system allows users to interact through a Telegram bot to get information, book, modify, or cancel appointments in a fully automated way.

---

## Overview

The platform is composed of three main components:

- **AI Agent**: Handles user interaction using LLMs and decides which actions to execute.
- **MCP Server**: Provides backend tools (calendar, email, database) exposed as APIs.
- **Telegram Bot**: User interface for real-time communication.

The architecture follows a modular and loosely coupled design, enabling scalability and easy integration of new services.

---

## Features

- Natural language interaction with users
- Appointment booking, modification, and cancellation
- Real-time availability checking
- Google Calendar integration
- Email notifications via Gmail API
- Customer data persistence
- Treatment information retrieval

---

## ⚙️ Architecture

### AI Agent
Handles conversation logic and tool orchestration using an LLM. It decides when to call backend services such as availability checks or booking confirmation. :contentReference[oaicite:0]{index=0}

### MCP Server
Implements backend services exposed as tools:
- Calendar management (Google Calendar)
- Email notifications (Gmail API)
- Customer data storage (JSON-based)
- Availability and booking logic :contentReference[oaicite:1]{index=1}

### Telegram Bot
Acts as the user interface, forwarding messages to the AI agent and returning responses in real time. :contentReference[oaicite:2]{index=2}

---

## Tech Stack

- **Python**
- **OpenAI API (LLM)**
- **FastMCP**
- **Google Calendar API**
- **Gmail API**
- **Telegram Bot API**
- **Docker (optional)**
- **JSON-based storage**

---

## Workflow

1. User sends a message via Telegram
2. The AI agent processes the request
3. If needed, it calls backend tools via the MCP server
4. The system interacts with:
   - Google Calendar (availability & booking)
   - Gmail (notifications)
   - Local storage (client data)
5. The response is sent back to the user

---

## 🔧 Setup & Execution

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables (.env)

```env
OPENAI_API_KEY=your_key
TELEGRAM_BOT_TOKEN=your_token
MCP_PORT=3333
TIMEZONE=Atlantic/Canary
GMAIL_FROM=your_email@gmail.com
```

### 3. Run MCP Server

```bash
python mcp_server.py
```

### 4. Run Telegram Bot

```bash
python telegram_bot.py
```
