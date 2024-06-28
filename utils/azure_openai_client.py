# Azure OpenAI Studio API client
import os
from openai import AzureOpenAI
from dotenv import load_dotenv


def initialize_client():
    """Initialize the Azure OpenAI client."""
    # Load environment variables
    load_dotenv()

    return AzureOpenAI(
        api_key=os.getenv('AZURE_API_KEY'),
        api_version="2024-05-01-preview",
        azure_endpoint="https://yarado-ai-v1.openai.azure.com/"
    )
