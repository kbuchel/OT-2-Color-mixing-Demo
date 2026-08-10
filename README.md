# OT-2-Color-mixing-Demo

## Project Overview

Automated colour-mixing platform
using an OT-2 Liquid Handler to prepare dye solutions and measure their respective light intensities.This project is to be used as an educational tool for self-driving labs (SDLs) in collaboration with the Acceleration Consortium.

The platform's components are:
- Opentrons OT-2 Liquid Handler to prepare 300 μl (food) dye solutions
- Red, Yellow, and Blue stock solutions (reservoirs), waste bottle + 3D-printed bottle holder
- Adafruit AS7341 sensor + Raspbery Pi Pico for spectral measurements
- Router for signal transmissions
- Hugging Face API for single experiment submissions
- Google Colab for batch submissions 
- Laptop
- MongoDB database (student ID's and experiment results can be found here)
- 96-well plate
- 96 300 μl tiprack
- 3d printed sensor housing + base

The following diagram outlines dataflow:



## Getting Started

**Note:** If this is your first time accessing the OT-2 via SSH, you will need to connect the robot to the laptop and use the wired IP for connecting.

1. Place the components in their respective locations (see the figure below as reference)
2. Power on the OT-2 and connect it to the router via LAN
3. SSH into the OT-2 using the wireless IP and run 'OT2mqtt_Aug_7.py' in 'var/lib/jupyter/notebooks'
4. Connect the Raspberry Pi Pico to the laptop via cable and run 'main.py' on the microcontroller
5. Access the repositiory for 'https://accelerationconsortium-ot-2-lcm.hf.space/' and restart the space

## Data + Color matching

The platform measures the light intensity of a respective sample with applications in colour matching using Bayesian Optimization:
