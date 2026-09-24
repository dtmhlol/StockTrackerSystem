import tkinter as tk
from tkinter import ttk, messagebox
import sqlite3
import os
from datetime import datetime

class StockTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Stock Tracker - Manager Dashboard")
        self.root.geometry("900x600")
        self.root.configure(bg="#f3f4f6") # Light gray background
        
        # Define database path (going up one level from src/ to root, then into database/)
        # For simplicity when running as an exe, we check the current directory or a database folder
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.db_path = os.path.join(base_dir, 'database', 'store_inventory.db')
        
        # Fallback to local directory if the structure isn't perfect (e.g., when run as .exe)
        if not os.path.exists(os.path.join(base_dir, 'database')):
            self.db_path = 'store_inventory.db'
            
        self.init_db()
        self.setup_ui()
        self.refresh_data()

    def init_db(self):
        """Ensures the database and required tables exist before launching the UI."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            # Create inventory table if it doesn't exist. 
            # This matches the data your mobile app sends via the vessel.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    barcode TEXT NOT NULL,
                    expiry_date TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            conn.close()
        except Exception as e:
            messagebox.showerror("Database Error", f"Could not connect to database: {e}")

    def setup_ui(self):
        """Sets up the visual elements of the application."""
        # Style Configuration
        style = ttk.Style()
        style.theme_use("clam") # 'clam' theme looks more modern on Windows
        
        # Configure Treeview colors and fonts
        style.configure("Treeview", 
                        background="#ffffff",
                        foreground="#374151",
                        rowheight=30,
                        fieldbackground="#ffffff",
                        font=('Inter', 10))
        style.configure("Treeview.Heading", 
                        font=('Inter', 11, 'bold'),
                        background="#f9fafb",
                        foreground="#111827")
        style.map('Treeview', background=[('selected', '#3b82f6')]) # Blue highlight

        # Top Frame (Header & Controls)
        top_frame = tk.Frame(self.root, bg="#ffffff", padx=20, pady=15)
        top_frame.pack(fill=tk.X, side=tk.TOP)
        
        # Title Label
        title_label = tk.Label(top_frame, text="Active Inventory Overview", 
                               font=("Inter", 16, "bold"), bg="#ffffff", fg="#1f2937")
        title_label.pack(side=tk.LEFT)

        # Action Buttons
        refresh_btn = tk.Button(top_frame, text="↻ Refresh Data", command=self.refresh_data,
                                bg="#10b981", fg="white", font=("Inter", 10, "bold"), 
                                relief=tk.FLAT, padx=15, pady=5, cursor="hand2")
        refresh_btn.pack(side=tk.RIGHT, padx=5)

        delete_btn = tk.Button(top_frame, text="🗑️ Remove Selected", command=self.delete_selected,
                               bg="#ef4444", fg="white", font=("Inter", 10, "bold"), 
                               relief=tk.FLAT, padx=15, pady=5, cursor="hand2")
        delete_btn.pack(side=tk.RIGHT, padx=5)

        # Main Frame for Data Table
        main_frame = tk.Frame(self.root, bg="#f3f4f6", padx=20, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Scrollbar
        tree_scroll = ttk.Scrollbar(main_frame)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Data Table (Treeview)
        columns = ("ID", "Barcode", "Expiry Date", "Quantity", "Status")
        self.tree = ttk.Treeview(main_frame, columns=columns, show="headings", yscrollcommand=tree_scroll.set)
        self.tree.pack(fill=tk.BOTH, expand=True)
        tree_scroll.config(command=self.tree.yview)

        # Define Headings and Columns widths
        self.tree.heading("ID", text="Sys ID")
        self.tree.column("ID", width=60, anchor=tk.CENTER)
        
        self.tree.heading("Barcode", text="Product Barcode")
        self.tree.column("Barcode", width=200, anchor=tk.W)
        
        self.tree.heading("Expiry Date", text="Expiry Date (YYYY-MM)")
        self.tree.column("Expiry Date", width=150, anchor=tk.CENTER)
        
        self.tree.heading("Quantity", text="Qty on Hand")
        self.tree.column("Quantity", width=100, anchor=tk.CENTER)
        
        self.tree.heading("Status", text="Status Flag")
        self.tree.column("Status", width=150, anchor=tk.CENTER)

        # Tag configurations for row coloring
        self.tree.tag_configure('expired', background='#fca5a5')    # Light Red
        self.tree.tag_configure('expiring_soon', background='#fde047') # Yellow
        self.tree.tag_configure('good', background='#ffffff')       # White

        # Legend Frame at the bottom
        legend_frame = tk.Frame(self.root, bg="#f3f4f6", padx=20, pady=10)
        legend_frame.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Label(legend_frame, text="Legend: ", font=("Inter", 10, "bold"), bg="#f3f4f6").pack(side=tk.LEFT)
        tk.Label(legend_frame, text="■ Expired ", fg="#ef4444", font=("Inter", 12, "bold"), bg="#f3f4f6").pack(side=tk.LEFT)
        tk.Label(legend_frame, text="■ Expiring in < 60 Days ", fg="#eab308", font=("Inter", 12, "bold"), bg="#f3f4f6").pack(side=tk.LEFT)

    def refresh_data(self):
        """Fetches data from SQLite and populates the Treeview, calculating expiry status."""
        # Clear existing data in the tree
        for item in self.tree.get_children():
            self.tree.delete(item)

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id, barcode, expiry_date, quantity FROM inventory ORDER BY expiry_date ASC")
            rows = cursor.fetchall()
            conn.close()

            current_date = datetime.now()

            for row in rows:
                db_id, barcode, expiry_str, qty = row
                
                # Default status
                status = "Good"
                tag = "good"
                
                try:
                    # Parse YYYY-MM (e.g. 2026-12) format from the mobile app
                    expiry_date = datetime.strptime(expiry_str, "%Y-%m")
                    
                    # Calculate days difference (approximating to the end of the month)
                    days_until_expiry = (expiry_date - current_date).days
                    
                    if days_until_expiry < 0:
                        status = "EXPIRED"
                        tag = "expired"
                    elif days_until_expiry < 60:  # 60 days warning window
                        status = "Expiring Soon"
                        tag = "expiring_soon"
                except ValueError:
                    status = "Invalid Date"
                    
                # Insert row into tree
                self.tree.insert("", tk.END, values=(db_id, barcode, expiry_str, qty, status), tags=(tag,))

        except Exception as e:
            messagebox.showerror("Error", f"Failed to load data: {e}")

    def delete_selected(self):
        """Deletes the selected row(s) from the SQLite database."""
        selected_items = self.tree.selection()
        
        if not selected_items:
            messagebox.showwarning("Warning", "Please select an item to remove.")
            return

        # Confirm deletion
        confirm = messagebox.askyesno("Confirm Removal", f"Are you sure you want to permanently remove {len(selected_items)} selected item(s) from inventory?")
        
        if confirm:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                for item in selected_items:
                    # Get the ID (first column) of the selected row
                    item_values = self.tree.item(item, 'values')
                    db_id = item_values[0]
                    
                    # Delete from database
                    cursor.execute("DELETE FROM inventory WHERE id=?", (db_id,))
                
                conn.commit()
                conn.close()
                
                # Refresh UI to show updated data
                self.refresh_data()
                messagebox.showinfo("Success", "Items successfully removed.")
                
            except Exception as e:
                messagebox.showerror("Database Error", f"Failed to delete items: {e}")

if __name__ == "__main__":
    # Create the main window and start the application loop
    root = tk.Tk()
    app = StockTrackerApp(root)
    
    # Optional: Set window icon (if you have an .ico file, uncomment line below)
    # root.iconbitmap('icon.ico')
    
    root.mainloop()