import asyncio
import os
import sys

# スクリプトがプロジェクトルートまたはサブディレクトリから実行されることを想定し、
# インポートが正しく機能するようにプロジェクトルートをsys.pathに追加します。
# このパスは、スクリプトの実行コンテキストに応じて調整が必要になる場合があります。
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "."))
sys.path.insert(0, project_root)

try:
    from app.llm.manager import LLMManager
    print("Successfully imported LLMManager.")
except ImportError as e:
    print(f"Failed to import LLMManager: {e}")
    sys.exit(1)

async def main():
    print("Initializing LLMManager...")
    manager = LLMManager()
    llm_provider = manager.get_current_provider()
    
    if llm_provider:
        print(f"Using LLM provider: {llm_provider.__class__.__name__}")
        try:
            print("Attempting chat completion...")
            response = await llm_provider.chat_completion(
                messages=[{"role": "user", "content": "Tell me a very short story."}],
                max_tokens=100, # 短い物語のためにmax_tokensを増やす
                temperature=0.7
            )
            print("LLM Response:")
            # OpenAIのChatCompletionのようなレスポンス構造を想定
            if response and response.choices and response.choices[0] and response.choices[0].message:
                print(response.choices[0].message.content)
            else:
                print("Unexpected response structure from LLM.")
        except Exception as e:
            print(f"Error interacting with LLM: {type(e).__name__}: {e}")
    else:
        print("No LLM provider found or initialized.")

if __name__ == "__main__":
    print("Starting LLM interaction test script...")
    asyncio.run(main())
    print("LLM interaction test script finished.")