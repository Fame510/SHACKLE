from setuptools import setup, find_packages

setup(
    name="shackle",
    version="0.1.0",
    description="A lightweight, framework-agnostic runtime circuit breaker for LLM agents.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Dante Bullock",
    author_email="docspoc101@gmail.com",
    license="AGPL-3.0",
    url="https://github.com/Fame510/SHACKLE",
    project_urls={
        "Bug Reports": "https://github.com/Fame510/SHACKLE/issues",
        "Commercial Licensing": "mailto:docspoc101@gmail.com",
    },
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
        "License :: OSI Approved :: GNU Affero General Public License v3 (AGPLv3)",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
