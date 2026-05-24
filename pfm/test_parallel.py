import asyncio
import time
import ollama

async def fetch_response(task_id, prompt):
    print(f"Task {task_id} started...")
    client = ollama.AsyncClient()
    
    # We use stream=False so it waits for the full response to finish
    response = await client.generate(model='qwen2:1.5b', prompt=prompt, stream=False)
    
    print(f"Task {task_id} finished! Length: {len(response['response'])} characters.")

async def main():
    start_time = time.time()
    
    # Define two tasks to run simultaneously
    task1 = fetch_response(1, "Explain the theory of relativity in simple terms.")
    task2 = fetch_response(2, "Write a 500 word essay about the importance of bees.")
    
    # asyncio.gather fires them off at the exact same time
    await asyncio.gather(task1, task2)
    
    end_time = time.time()
    print(f"\nTotal time for both requests: {end_time - start_time:.2f} seconds")

if __name__ == "__main__":
    asyncio.run(main())