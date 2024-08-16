# Azure OpenAI Studio API client
import os
from openai import AzureOpenAI, OpenAI


def initialize_client():
    """Initialize the Azure OpenAI client."""

    return AzureOpenAI(
        api_key=os.getenv('AZURE_API_KEY'),
        api_version="2024-05-01-preview",
        azure_endpoint="https://yarado-ai-v1.openai.azure.com/"
    )


def initialize_openai_client():
    """Initialize the OpenAI client for staging environment."""
    return OpenAI(
        api_key=os.getenv('OPENAI_API_KEY')
    )