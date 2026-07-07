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
from fastapi.responses import StreamingResponse

Memory=MemorySaver()
load_dotenv()

search_tool=TavilySearch(
    max_results=5
)

tools=[search_tool]

llm=ChatGoogleGenerativeAI(model="gemini-2.5-flash")
llm_with_tool=llm.bind_tools(tools=tools)

prompt=ChatPromptTemplate([
    ("system","You are an AI assistant. Chat with the user in a very "
    "friendly and humanly way. Help the user and answer user queries. "
    "Only use tools if the answer to the user quries is not found "
    "in the chat history."),
    MessagesPlaceholder(variable_name="messages")
])

llm_chain=prompt | llm_with_tool

async def llm_node(state:MessagesState):
    result=await llm_chain.ainvoke(state)
    return {"messages":[result]}

async def tool_router(state:MessagesState):
    lastAIMessage=state["messages"][-1]
    if isinstance(lastAIMessage,AIMessage) and hasattr(lastAIMessage,"tool_calls") and len(lastAIMessage.tool_calls)>0:
        return "tool_executer"
    else:
        return END
  
graph=StateGraph(MessagesState)
graph.add_node("llm_executer",llm_node)
graph.add_node("tool_executer",ToolNode(tools=tools))

graph.add_edge("tool_executer","llm_executer")
graph.add_conditional_edges(
    "llm_executer",tool_router
)

graph.set_entry_point("llm_executer")

agent=graph.compile(checkpointer=Memory)

app = FastAPI()

# Add CORS middleware with settings that match frontend requirements
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"], 
    expose_headers=["Content-Type"], 
)

async def generate_respose(message:str,checkpoint_id:Optional[str]=None):
    if not checkpoint_id:
        new_checkpoint_id=str(uuid4())

        config={
            "configurable":{
                "thread_id":new_checkpoint_id
            }
        }

        events=agent.astream_events({
            "messages":[HumanMessage(content=message)]
        },version="v2",config=config)

        yield f"data: {{\"type\": \"checkpoint\", \"checkpoint_id\": \"{new_checkpoint_id}\"}}\n\n"
    
    else:
        config={
            "configurable":{
                "thread_id":checkpoint_id
            }
        }

        events=agent.astream_events({
            "messages":[HumanMessage(content=message)]
        },version="v2",config=config)

    async for event in events:
        event_type=event["event"]
        
        if event_type=="on_chat_model_stream":
            content=event["data"]["chunk"].content
            if isinstance(content,list)and len(content)>0:
                content=content[0]["text"]

            if content:
                safe_content=content.replace('"','\\"').replace("\n","\\n")
                yield f"data: {{\"type\":\"content\",\"content\":\"{safe_content}\"}}\n\n"

        elif event_type=="on_chat_model_end":
            if hasattr(event["data"]["output"],"tool_calls"):
                # print("**********************************")
                tool_calls=event["data"]["output"].tool_calls
            else:
                tool_calls=[]
            
            search_calls = [call for call in tool_calls if call["name"] == "tavily_search"]
            
            # print(f"Search calls {search_calls}")
            # print("**********************************")
            # print(f"tool calls {tool_calls}")
            # print("**********************************")
            # print(event["data"]["output"])

            if search_calls:
                # print("**********************************")
                search_query = search_calls[0]["args"].get("query", "")
                safe_query = search_query.replace('"', '\\"').replace("'", "\\'").replace("\n", "\\n")
                yield f"data: {{\"type\":\"search_start\",\"query\":\"{safe_query}\"}}\n\n"        
        
        

        elif event_type=="on_tool_end":
            output = event["data"]["output"].content
            
            try:
                output = json.loads(output)
                print(output)
                if isinstance(output, list):
                    for out in output:
                        url = [item["url"] for item in out if isinstance(item, dict) and "url" in item['results']]
                        urls_json = json.dumps(url)
                        yield f"data: {{\"type\": \"search_results\", \"urls\": {urls_json}}}\n\n"
                else:
                    url = [item['url'] for item in output['results'] if isinstance(item, dict) and "url" in item]
                    urls_json = json.dumps(url)
                    yield f"data: {{\"type\": \"search_results\", \"urls\": {urls_json}}}\n\n"
            except json.JSONDecodeError:
                pass

    # if event_type=="on_chain_end":
    #     check=event["data"]["output"]
    #     if isinstance(check,dict):
    #         print(event["data"]["output"]['messages'])
    # print(events)  
    yield f"data: {{\"type\": \"end\"}}\n\n"

@app.get("/")
async def chat_stream(message: str, checkpoint_id: Optional[str] = Query(None)):
    return StreamingResponse(
        generate_respose(message, checkpoint_id), 
        media_type="text/event-stream"
    )
