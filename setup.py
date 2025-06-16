from setuptools import setup, find_packages
import os

def load_requirements(filename='requirements.txt'):
    with open(filename, 'r', encoding='utf-8') as f:
        requirements = []
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('git+'):
                requirements.append(line)
    return requirements

# Read the contents of your README file for a long description
this_directory = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(this_directory, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

setup(
    name='garmin',  # Update to your project's name
    version='0.1.0',
    description='A project for pulling, processing, and visualizing Garmin data.',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Your Name',
    author_email='your.email@example.com',
    url='https://github.com/pwilliams272/garmin',  # Update URL if applicable
    packages=find_packages(),
    include_package_data=True,
    install_requires=load_requirements(),  # Loads requirements from requirements.txt
    entry_points={
        'console_scripts': [
            'garmin=garmin.app.app:main',
        ],
    },
    classifiers=[
        'Programming Language :: Python :: 3.11',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.11',
)
