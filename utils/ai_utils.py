import logging
from openai import AzureOpenAI
import random
import time
import asyncio


async def vectorize_text(client, text, max_retries=5, initial_timeout=1, max_timeout=60):
    logging.info(f'Calling upon {client}')
    for attempt in range(max_retries):
        try:
            response = client.embeddings.create(
                model="text-embedding-3-large",
                input=text,
                dimensions=3072
            )
            return response.data[0].embedding
        except AzureOpenAI as e:
            if attempt == max_retries - 1:
                logging.error(f"Max retries reached for vectorization. Last error: {e}")
                raise e

            wait_time = min(initial_timeout * (2 ** attempt) + random.uniform(0, 1), max_timeout)
            logging.warning(
                f"Vectorization attempt {attempt + 1} failed. Retrying in {wait_time:.2f} seconds. Error: {e}")
            await asyncio.sleep(wait_time)

    raise Exception(":warning: Unexpected error occurred during vectorization.")


def retry_request_openai(client, messages, model="gpt-4o-2024-08-06", max_retries=5, initial_timeout=1, max_timeout=60,
                         max_tokens=4096, json_schema=None):
    logging.info(f'Calling upon {client}')
    for attempt in range(max_retries):
        try:
            if json_schema:
                response_format = {
                    "type": "json_schema",
                    "json_schema": json_schema
                }
            else:
                response_format = {"type": "text"}
            logging.info(f"Attempt {attempt + 1} of {max_retries}...")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.2,
                response_format=response_format,
                timeout=90,
                seed=42
            )
            logging.info(f"Request successful on attempt {attempt + 1}")
            ai_generated_content = response.choices[0].message.content
            return ai_generated_content
        except AzureOpenAI as e:
            if attempt == max_retries - 1:
                logging.error(f"Max retries reached. Last error: {e}")
                error_message = f":warning: Error: OpenAI did not respond successfully after multiple attempts. \n\nLast error: \n```{str(e)}```\n\nPlease try again later."
                return error_message

            wait_time = min(initial_timeout * (2 ** attempt) + random.uniform(0, 1), max_timeout)
            logging.warning(f"Attempt {attempt + 1} failed. Retrying in {wait_time:.2f} seconds. Error: {e}")
            time.sleep(wait_time)

    return ":warning: Unexpected error occurred during API request."
