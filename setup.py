from setuptools import setup, find_packages

setup(
    name="whiskacli",
    version="1.2.0",
    description="Whiska - Command-line client for Adaptix C2 Framework",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "requests>=2.28",
        "websocket-client>=1.5",
        "rich>=12.0",
        "prompt_toolkit>=3.0",
        "cryptography>=41.0",
    ],
    entry_points={
        "console_scripts": [
            "whiska=whiskacli.__main__:main",
        ],
    },
)
