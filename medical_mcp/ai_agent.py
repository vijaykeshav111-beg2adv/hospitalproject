"""Groq + FastMCP agent from the original workshop, repaired for the merged project."""
from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from groq import Groq
from fastmcp import Client

load_dotenv()

GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/mcp")

groq = Groq(api_key=os.getenv("GROQ_API_KEY", ""))


async def ask_agent(question: str):
    async with Client(MCP_SERVER_URL) as client:
        tools = await client.list_tools()
        groq_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            }
            for tool in tools
        ]

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Vijay Vargiya Group of Hospitals assistant. "
                    "Use the available MCP tools for doctor or patient information. "
                    "Do not invent database information."
                ),
            },
            {"role": "user", "content": question},
        ]

        response = groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            tools=groq_tools,
            tool_choice="auto",
        )
        message = response.choices[0].message

        if not message.tool_calls:
            return {"answer": message.content or "", "tools_used": []}

        messages.append(message.model_dump(exclude_none=True))
        tools_used = []

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments or "{}")
            result = await client.call_tool(tool_name, arguments)
            tool_result = result.data if hasattr(result, "data") else str(result)
            tools_used.append(tool_name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": json.dumps(tool_result, default=str),
                }
            )

        final_response = groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            tool_choice="none",
        )
        return {
            "answer": final_response.choices[0].message.content or "",
            "tools_used": tools_used,
        }
