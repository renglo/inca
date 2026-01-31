"""
Inca Package
Travel/trip intent handlers: Runner, Reducer, Applier, Patcher, Tools.
"""

from setuptools import setup, find_packages

setup(
    name="inca",
    version="1.0.0",
    description="Inca travel/trip intent handlers (Runner, Reducer, Applier, Patcher, Tools)",
    author="EXHQ Team",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[],
    include_package_data=True,
    package_data={
        "inca": ["handlers/*.md"],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
