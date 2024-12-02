# Azure OpenAI Studio API client
import os
from openai import AzureOpenAI


def initialize_client():
    """Initialize the Azure OpenAI client."""

    return AzureOpenAI(
        api_key=os.getenv('AZURE_API_KEY'),
        api_version="2024-09-01-preview",
        azure_endpoint="https://yarado-ai-v1.openai.azure.com/"
    )


