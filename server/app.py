from langgraph.graph import add_messages, StateGraph, END, MessagesState
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage, AIMessageChunk
from dotenv import load_dotenv
from langchain_tavily import TavilySearch
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.prebuilt import ToolNode
from langgraph.types import Command
from langgraph.checkpoint.memory import MemorySaver
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from uuid import uuid4
import json
import os
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse


Memory = MemorySaver()
load_dotenv()

search_tool = TavilySearch(max_results=5)
tools = [search_tool]

llm = ChatGoogleGenerativeAI(model="gemini-3.5-flash")
llm_with_tool = llm.bind_tools(tools=tools)

prompt = ChatPromptTemplate([
    ("system", "You are an AI assistant. Chat with the user in a very "
               "friendly and human way. Help the user and answer user queries. "
               "Only use tools if the answer to the user queries is not found "
               "in the chat history."),
    MessagesPlaceholder(variable_name="messages")
])

llm_chain = prompt | llm_with_tool

async def llm_node(state: MessagesState):
    result = await llm_chain.ainvoke(state)
    return {"messages": [result]}

async def tool_router(state: MessagesState):
    lastAIMessage = state["messages"][-1]
    if isinstance(lastAIMessage, AIMessage) and hasattr(lastAIMessage, "tool_calls") and len(lastAIMessage.tool_calls) > 0:
        return "tool_executer"
    else:
        return END
  
graph = StateGraph(MessagesState)
graph.add_node("llm_executer", llm_node)
graph.add_node("tool_executer", ToolNode(tools=tools))

graph.add_edge("tool_executer", "llm_executer")
graph.add_conditional_edges("llm_executer", tool_router)
graph.set_entry_point("llm_executer")

agent = graph.compile(checkpointer=Memory)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"], 
    expose_headers=["Content-Type"], 
)

async def generate_response(message: str, checkpoint_id: Optional[str] = None):
    if not checkpoint_id:
        new_checkpoint_id = str(uuid4())
        config = {"configurable": {"thread_id": new_checkpoint_id}}
        
        events = agent.astream_events({
            "messages": [HumanMessage(content=message)]
        }, version="v2", config=config)

        yield f"data: {{\"type\": \"checkpoint\", \"checkpoint_id\": \"{new_checkpoint_id}\"}}\n\n"
    else:
        config = {"configurable": {"thread_id": checkpoint_id}}
        events = agent.astream_events({
            "messages": [HumanMessage(content=message)]
        }, version="v2", config=config)

    async for event in events:
        event_type = event["event"]
        
        if event_type == "on_chat_model_stream":
            content = event["data"]["chunk"].content
            if isinstance(content, list) and len(content) > 0:
                content = content[0]["text"]

            if content:
                safe_content = content.replace('"', '\\"').replace("\n", "\\n")
                yield f"data: {{\"type\":\"content\",\"content\":\"{safe_content}\"}}\n\n"

        elif event_type == "on_chat_model_end":
            if hasattr(event["data"]["output"], "tool_calls"):
                tool_calls = event["data"]["output"].tool_calls
            else:
                tool_calls = []
            
            search_calls = [call for call in tool_calls if call["name"] == "tavily_search"]

            if search_calls:
                search_query = search_calls[0]["args"].get("query", "")
                safe_query = search_query.replace('"', '\\"').replace("'", "\\'").replace("\n", "\\n")
                yield f"data: {{\"type\":\"search_start\",\"query\":\"{safe_query}\"}}\n\n"        

        elif event_type == "on_tool_end":
            output_content = event["data"]["output"].content
            try:
                # Tavily returns a JSON string containing an array of dictionaries
                results = json.loads(output_content)
                if isinstance(results, list):
                    urls = [item["url"] for item in results if isinstance(item, dict) and "url" in item]
                    urls_json = json.dumps(urls)
                    yield f"data: {{\"type\": \"search_results\", \"urls\": {urls_json}}}\n\n"
            except json.JSONDecodeError:
                pass

    yield f"data: {{\"type\": \"end\"}}\n\n"

@app.get("/chat/{message}")
async def chat_stream(message: str, checkpoint_id: Optional[str] = Query(None)):
    return StreamingResponse(
        generate_response(message, checkpoint_id), 
        media_type="text/event-stream"
    )

FRONTEND_BUILD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client", "out"))

# @app.get("/{full_path:path}")
# async def serve_frontend(full_path: str):
#     # Construct the absolute path to the requested file
#     file_path = os.path.join(FRONTEND_BUILD_DIR, full_path)
    
#     # If the file exists (e.g., a .js, .css, or image file), serve it directly
#     if os.path.isfile(file_path):
#         return FileResponse(file_path)
    
#     # If the file doesn't exist, it's likely a client-side route (like /dashboard).
#     # Fall back to serving index.html and let the frontend handle the routing.
#     index_path = os.path.join(FRONTEND_BUILD_DIR, "index.html")
#     return FileResponse(index_path)

next_assets_dir = os.path.join(FRONTEND_BUILD_DIR, "_next")
if os.path.exists(next_assets_dir):
    app.mount("/_next", StaticFiles(directory=next_assets_dir), name="next-assets")

@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    file_path = os.path.join(FRONTEND_BUILD_DIR, full_path)
    
    if os.path.isfile(file_path):
        media_type = None
        if full_path.endswith(".css"):
            media_type = "text/css"
        elif full_path.endswith(".js"):
            media_type = "application/javascript"
            
        return FileResponse(file_path, media_type=media_type)
    
    index_path = os.path.join(FRONTEND_BUILD_DIR, "index.html")
    return FileResponse(index_path)