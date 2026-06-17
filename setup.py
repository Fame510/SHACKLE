from setuptools import setup, find_packages

setup(
    name="shackle-guard",
    version="0.1.0",
    description="A lightweight, framework-agnostic runtime circuit breaker for LLM agents.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Dante Bullock",
    author_email="engineering@sovereignlogic.io",
    license="AGPL-3.0",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "rich>=13.0.0",
        "pyyaml>=6.0.0",
    ],
    extras_require={
        "dev": ["pytest>=7.0.0"],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
