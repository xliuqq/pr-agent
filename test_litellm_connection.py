"""
Test script to verify LiteLLM backend service connectivity.

This script performs a simple chat completion request to test if the LiteLLM
backend service is properly configured and accessible.

Usage:
    python tests/health_test/test_litellm_connection.py

Environment variables:
    - LOG_LEVEL: Set logging level (default: INFO)
    - TEST_MODEL: Override the model to use for testing (optional)
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger, setup_logger

# Setup logging
log_level = os.environ.get("LOG_LEVEL", "INFO")
setup_logger(log_level)
logger = get_logger()


async def test_litellm_connection():
    """
    Test LiteLLM backend service connectivity with a simple chat completion.
    
    Returns:
        bool: True if connection successful, False otherwise
    """
    logger.info("=" * 80)
    logger.info("Testing LiteLLM Backend Service Connection")
    logger.info("=" * 80)
    
    try:
        # Get configuration
        model = os.getenv('TEST_MODEL', None)
        if not model:
            model = get_settings().config.model
        
        logger.info(f"Using model: {model}")
        logger.info(f"API Base: {get_settings().get('OPENAI.API_BASE', 'Not set')}")
        
        # Initialize LiteLLM handler
        logger.info("\nInitializing LiteLLM AI Handler...")
        handler = LiteLLMAIHandler()
        logger.info("✓ LiteLLM handler initialized successfully")
        
        # Prepare test messages
        system_message = "You are a helpful assistant. Please respond with 'OK' to confirm connectivity."
        user_message = "你能做什么功能？"
        
        logger.info("\nSending test chat completion request...")
        logger.info(f"System: {system_message}")
        logger.info(f"User: {user_message}")
        
        # Send test request
        response, finish_reason = await handler.chat_completion(
            model=model,
            system=system_message,
            user=user_message,
            temperature=0.1,
        )
        
        # Validate response
        if response:
            logger.info("\n" + "=" * 80)
            logger.info("✓ LiteLLM Connection Test PASSED")
            logger.info("=" * 80)
            logger.info(f"Response received: {response[:200]}...")
            logger.info(f"Finish reason: {finish_reason}")
            logger.info("\nLiteLLM backend service is working correctly!")
            return True
        else:
            logger.error("\n" + "=" * 80)
            logger.error("✗ LiteLLM Connection Test FAILED")
            logger.error("=" * 80)
            logger.error("No response received from LiteLLM service")
            return False
            
    except Exception as e:
        logger.error("\n" + "=" * 80)
        logger.error("✗ LiteLLM Connection Test FAILED")
        logger.error("=" * 80)
        logger.error(f"Error type: {type(e).__name__}")
        logger.error(f"Error message: {str(e)}")
        logger.exception("Detailed exception information:")
        return False


def main():
    """Main entry point for the test script."""
    try:
        result = asyncio.run(test_litellm_connection())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        logger.info("\nTest interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
