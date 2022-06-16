from setuptools import find_packages, setup

with open('requirements.txt') as f:
    required = f.read().splitlines()

# required.append('jesse @ git+https://github.com/ysdede/jesse.git@cache+yakirsim#egg=jesse',)

setup(
    name="strat",
    version='0.1.8',
    packages=find_packages(),
    install_requires=required,

    entry_points=None,
    python_requires='>=3.7',
    include_package_data=True,
)
