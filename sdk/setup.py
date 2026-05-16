from setuptools import find_packages, setup

setup(
    name="agentops-sdk",
    version="1.0.0",
    description="AgentOps Python SDK — instrument your AI agents.",
    long_description=open("README.md").read() if __import__("os").path.exists("README.md") else "",
    long_description_content_type="text/markdown",
    author="AgentOps",
    packages=find_packages(),
    install_requires=[
        "httpx>=0.27",
    ],
    python_requires=">=3.9",
)
