import os
import time
import PySimpleGUI as sg
import requests
from zeep import Client
from bs4 import BeautifulSoup

# Create the loading window
loading_layout = [
    [sg.Text("Loading...")],
    [sg.ProgressBar(100, orientation="h", size=(20, 20), key="-PROGRESS-")]
]

loading_window = sg.Window("Loading", loading_layout)

# Simulate loading progress
for i in range(100):
    event, values = loading_window.read(timeout=10)
    if event == sg.WINDOW_CLOSED:
        break
    loading_window["-PROGRESS-"].update(i + 1)

# Retrieve available DocTypes from the website
url = "https://peppol.helger.com/public/menuitem-validation-ws2"
response = requests.get(url)
soup = BeautifulSoup(response.content, "html.parser")
doc_types = soup.find_all("li")

# Extract DocType values and names
doc_type_values = [doc_type.code.text for doc_type in doc_types if doc_type.code]
doc_type_names = [doc_type.text.split(" - ")[-1] for doc_type in doc_types if doc_type.code]

# Close the loading window
loading_window.close()

# Create the GUI layout
layout = [
    [sg.Text("Enter the file path:")],
    [sg.Input(key="-FILE_PATHS-"), sg.FilesBrowse()],
    [sg.Text("Please select a DocType:")],
    [sg.Input(key="-FILTER-", enable_events=True)],
    [sg.Checkbox("Exclude deprecated", key="-EXCLUDE_DEPRECATED-", enable_events=True)],
    [sg.Listbox(doc_type_names, size=(150, 30), key="-DOCTYPES-", enable_events=True)],
    [sg.Text("Please select a folder to save results (a window will ope for each result if left blank):")],
    [sg.Input(key="-FOLDER-"), sg.FolderBrowse()],
    [sg.Button("Validate")]
]

# Create the window
window = sg.Window("PHAX's File Validation - by @KbraBass", layout, resizable=True)

while True:
    event, values = window.read()

    if event == sg.WINDOW_CLOSED:
        break

    if event == "-FILTER-":
        filter_text = values["-FILTER-"]
        exclude_deprecated = values["-EXCLUDE_DEPRECATED-"]
        filtered_doc_types = [
            doc_type_name for doc_type_name in doc_type_names if filter_text.lower() in doc_type_name.lower()
        ]
        if exclude_deprecated:
            filtered_doc_types = [
                doc_type_name for doc_type_name in filtered_doc_types if "(Deprecated)" not in doc_type_name
            ]
        window["-DOCTYPES-"].update(filtered_doc_types)

    if event == "-EXCLUDE_DEPRECATED-":
        exclude_deprecated = values["-EXCLUDE_DEPRECATED-"]
        filter_text = values["-FILTER-"]
        filtered_doc_types = [
            doc_type_name for doc_type_name in doc_type_names if filter_text.lower() in doc_type_name.lower()
        ]
        if exclude_deprecated:
            filtered_doc_types = [
                doc_type_name for doc_type_name in filtered_doc_types if "(Deprecated)" not in doc_type_name
            ]
        window["-DOCTYPES-"].update(filtered_doc_types)

    if event == "Validate":
        doc_type_index = doc_type_names.index(values["-DOCTYPES-"][0])
        doc_type_value = doc_type_values[doc_type_index]

        # Split the file_paths string into a list of filenames
        file_paths = values["-FILE_PATHS-"].split(";")

        # Get the folder to save results
        folder_path = values["-FOLDER-"]

        # Create a new window with a progress bar
        progress_layout = [
            [sg.Text(f"Processing file 0 out of {len(file_paths)}", key="-PROGRESS_TEXT-")],  # Add a key to the Text element
            [sg.ProgressBar(len(file_paths), orientation="h", size=(20, 20), key="-PROGRESS-")],
            [sg.Text("", key="-ETA-", size=(30, 1))],  # ETA text
        ]
        progress_window = sg.Window("Processing", progress_layout, finalize=True)

        start_time = time.time()  # Record the start time

        # Send a SOAP request for each file
        for i, file_path in enumerate(file_paths):
            with open(file_path, "r") as file:
                xml_payload = file.read()

            wsdl_url = "https://peppol.helger.com/wsdvs?wsdl"

            client = Client(wsdl_url)
            response = client.service.validate(XML=xml_payload, VESID=doc_type_value)

            # Check if a folder path is provided
            if folder_path:
                # Save the result to a file
                result_file_path = os.path.join(folder_path, os.path.basename(file_path) + "_results.txt")
                with open(result_file_path, "w") as result_file:
                    result_file.write(str(response))  # Convert the response to a string
            else:
                # Create the result window
                result_layout = [
                    [sg.Multiline(response, size=(80, 20), key="-TEXT-", enable_events=True)],
                    [sg.Button("Copy")]
                ]

                result_window = sg.Window("Validation Result for " + os.path.basename(file_path), result_layout)

                while True:
                    result_event, result_values = result_window.read()

                    if result_event == sg.WINDOW_CLOSED:
                        break

                    if result_event == "-TEXT-":
                        result_window["-TEXT-"].Widget.config(wrap="none")

                    if result_event == "Copy":
                        result_window["-TEXT-"].Widget.clipboard_clear()
                        result_window["-TEXT-"].Widget.clipboard_append(result_values["-TEXT-"])

                result_window.close()

            # Update the progress text and bar
            progress_window["-PROGRESS_TEXT-"].update(f"Processing file {i + 1} out of {len(file_paths)}")
            progress_window["-PROGRESS-"].update(i + 1)

            # Calculate and update the ETA
            elapsed_time = time.time() - start_time
            eta = elapsed_time / (i + 1) * (len(file_paths) - i - 1)
            progress_window["-ETA-"].update(f"ETA: {eta:.2f} seconds")

        # Close the progress window
        progress_window.close()

window.close()