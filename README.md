# DocumentValidator
Simple Electronic Document Validation tool that uses Phax' web service with a GUI.

The WebService is described on https://peppol.helger.com/public/menuitem-validation-ws2  
The original validation engine is on https://github.com/phax/phive  
My code is just a simple GUI to interact with Helger's validation tool. It does the same job as [ecosio](https://ecosio.com/en/peppol-and-xml-document-validator/), but without needing a browser.

## Running from the source code
The program is written in Python and should work on any platform as long as all dependencies are installed. The following libraries are needed:
* bs4
* zeep
* requests
* PySimpleGUI

## Running on Windows
A pre-compiled version in a single *.exe file is available for Windows. I have compiled and tested it only on Windows 11 using PyInstaller.  
The binary is not digitally signed, so it may be flagged by your anti-virus as potentially malitious.

## Other considerations
If you like and use this program, please leave a comment tellimg me what you like and what improvement you'd like to see.  
The back-end is not my work, so all credit goes to @phax and the PEPPOL team.

## Known Issues
There is no error message for when the WebService is down. So the document type list simply doesn't get populated.
