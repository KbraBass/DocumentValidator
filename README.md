# DocumentValidator
Simple Electronic Document Validation tool that uses Phax' web service with a GUI.

The WebService is described on https://peppol.helger.com/public/menuitem-validation-ws2  
The original validation engine is on https://github.com/phax/phive  
My code is just a simple GUI to interact with Helger's validation tool. It does the same job as [ecosio](https://ecosio.com/en/peppol-and-xml-document-validator/), but without needing a browser.

If you'd like to implement a similar solution in a production environment, if you're getting lots of error 429 responses or if you use this in a daily basis, you should probably contact Philip Helger for support as he owns the back-end. his disclaimer reads: 
> This service is currently provided free of charge. If you want to setup your own service for production usage don't hesitate to contact me at philip[at]helger[dot]com for support.

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
If you like and use this program, please leave a comment telling me what you like and what improvement you'd like to see.  
The back-end is not my work, so all credit goes to @phax and the PEPPOL team.

## Known Issues
There is no error message for when the WebService is down. So the document type list simply doesn't get populated.

## Updates and Releases
2024-02-26 Added support to select and validate multiple files at once.  
2024-02-26 Added progress bar and ETA for when validating multiple files.  
2024-02-26 Added support to save the validation results to a folder instead of displaying a window. The files will be saved as "[sourceFilename]_results.txt".
