import sqlite3

# Connect to the SQLite database
conn = sqlite3.connect('maps.db')

# Create a cursor object
cursor = conn.cursor()

# Execute a query to fetch all records from the Recording table
cursor.execute("SELECT * FROM Recording")

# Fetch all results
rows = cursor.fetchall()

# Print the results
for row in rows:
    print(row)

# Close the connection
conn.close()