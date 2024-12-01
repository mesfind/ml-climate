import numpy as np
import random
import matplotlib.pyplot as plt
import pandas as pd
import torch
import torch.nn as nn
from torch.autograd import Variable
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import mean_squared_error, r2_score
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
from sklearn.preprocessing import MinMaxScaler

# Set seed for reproducibility
seed = 42
np.random.seed(seed)
random.seed(seed)
torch.manual_seed(seed)
if torch.cuda.is_available():
    torch.cuda.manual_seed_all(seed)

plt.style.use("ggplot")

# Load the dataset
df = pd.read_csv('data/weather_forecast.csv')


# Clean the 'DateTime' column by removing malformed entries
df = df[df['DateTime'].str.match(r'\d{4}-\d{2}-\d{2}.*')]

# Convert 'DateTime' column to datetime format, allowing pandas to infer the format
df["DateTime"] = pd.to_datetime(df["DateTime"], errors='coerce')

# Drop rows where the 'DateTime' conversion resulted in NaT (not-a-time)
df.dropna(subset=["DateTime"], inplace=True)

# Reindex the DataFrame before splitting
df.set_index('DateTime', inplace=True)

# select only important features
features = ['p(mbar)','T(degC)', 'VPmax(mbar)','VPdef(mbar)', 'sh(g/kg)', 'rho(g/m**3)',  'wv(m/s)', 'wd(deg)' ]
df = df[features]

# Resample the DataFrame by day and compute the mean for each day
df_daily = df.resample('D').mean()


class SlidingWindowGenerator:
    def __init__(self, seq_length, label_width, shift, df, label_columns=None, dropnan=True):
        self.df = df
        self.label_columns = label_columns
        self.dropnan = dropnan

        if label_columns is not None:
            self.label_columns_indices = {name: i for i, name in enumerate(label_columns)}
        self.column_indices = {name: i for i, name in enumerate(df.columns)}

        self.seq_length = seq_length
        self.label_width = label_width
        self.shift = shift
        self.total_window_size = seq_length + shift

        self.input_slice = slice(0, seq_length)
        self.input_indices = np.arange(self.total_window_size)[self.input_slice]

        self.label_start = self.total_window_size - label_width
        self.labels_slice = slice(self.label_start, None)
        self.label_indices = np.arange(self.total_window_size)[self.labels_slice]

    def __repr__(self):
        return '\n'.join([
            f'Total window size: {self.total_window_size}',
            f'Input indices: {self.input_indices}',
            f'Label indices: {self.label_indices}',
            f'Label column name(s): {self.label_columns}'])

    def sliding_windows(self):
        data = self.df.values
        X, y = [], []

        for i in range(len(data) - self.total_window_size + 1):
            input_window = data[i:i + self.seq_length]
            label_window = data[i + self.seq_length:i + self.total_window_size]

            # Check for nan values in input_window and label_window by flattening them
            if np.isnan(input_window.flatten()).any() or np.isnan(label_window.flatten()).any():
                continue  # Skip this window if it contains nan values

            X.append(input_window)

            if self.label_columns is not None:
                label_window = label_window[:, [self.column_indices[name] for name in self.label_columns]]
            y.append(label_window)

        X, y = np.array(X), np.array(y)

        return X, y[:,-1,]


# Initialize the generator
# if label_width=1 it will be single-step forecasting
swg = SlidingWindowGenerator(seq_length=30, label_width=3, shift=1, df=df_daily, label_columns=['wv(m/s)'])

# Generate windows
X, y = swg.sliding_windows()

# Normalize the data
scaler_X = MinMaxScaler()
scaler_y = MinMaxScaler()

X_shape = X.shape
y_shape = y.shape

X_flat = X.reshape(-1, X_shape[-1])
y_flat = y.reshape(-1, y_shape[-1])

X = scaler_X.fit_transform(X_flat).reshape(X_shape)
y = scaler_y.fit_transform(y_flat).reshape(y_shape)

# train and test data loading in tensor format
train_size = int(len(y) * 0.7)
test_size = len(y) - train_size

X_train = Variable(torch.Tensor(np.array(X[0:train_size])))
y_train = Variable(torch.Tensor(np.array(y[0:train_size])))

X_test = Variable(torch.Tensor(np.array(X[train_size:len(X)])))
y_test = Variable(torch.Tensor(np.array(y[train_size:len(y)])))

# Create TensorDataset instances for training and testing data
train_data = TensorDataset(X_train, y_train)
test_data = TensorDataset(X_test, y_test)

# Initialize DataLoader objects for both datasets with batch size 256
batch_size = 256
train_loader = DataLoader(train_data, batch_size=batch_size, shuffle=True)
test_loader = DataLoader(test_data, batch_size=batch_size, shuffle=False)

class LSTM(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers, output_size):
        super(LSTM, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True)
        self.fc = nn.Linear(hidden_size, output_size)

    def forward(self, x):
        h0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size)
        c0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size)
        
        out, _ = self.lstm(x, (h0, c0))
        out = self.fc(out[:, -1, :])
        return out


# Check for GPU availability including CUDA and Apple's MPS GPU
device = torch.device('cuda:0' if torch.cuda.is_available() else 'mps:0' if torch.backends.mps.is_available() else 'cpu')

# Training the model
num_epochs = 21
learning_rate = 0.01

input_size = X.shape[2] # feature fecture 
hidden_size = 5
num_layers = 2
output_size = y.shape[1]
lstm = LSTM(input_size, hidden_size, num_layers, output_size)
lstm.to(device)
criterion = torch.nn.MSELoss()    # Mean-squared error for regression
optimizer = torch.optim.Adam(lstm.parameters(), lr=learning_rate)

# Train the model
train_losses = []
test_losses = []
for epoch in range(num_epochs):
    # Train
    lstm.train()
    for inputs, targets in train_loader:
        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()
        outputs = lstm(inputs)
        train_loss = criterion(outputs, targets)
        train_loss.backward()
        optimizer.step()
        train_losses.append(train_loss.item())
    
    # Test
    lstm.eval()
    with torch.no_grad():
        for inputs, targets in test_loader:
            inputs, targets = inputs.to(device), targets.to(device)
            test_outputs = lstm(inputs)
            test_loss = criterion(test_outputs, targets)
            test_losses.append(test_loss.item())
    
    if epoch % 100 == 0:
        print(f"Epoch: {epoch}, Train Loss: {np.mean(train_losses[-len(train_loader):]):.5f}, Test Loss: {np.mean(test_losses[-len(test_loader):]):.5f}")

# Compute final MSE and R² for train and test
# Compute final MSE and R² for train and test sets
train_predict = lstm(X_train).data.numpy()
test_predict = lstm(X_test).data.numpy()

trainY_plot = y_train.data.numpy()
testY_plot = y_test.data.numpy()

train_predict = scaler_y.inverse_transform(train_predict)
trainY_plot = scaler_y.inverse_transform(trainY_plot)

test_predict = scaler_y.inverse_transform(test_predict)
testY_plot = scaler_y.inverse_transform(testY_plot)

train_mse = mean_squared_error(trainY_plot, train_predict)
train_r2 = r2_score(trainY_plot, train_predict)

test_mse = mean_squared_error(testY_plot, test_predict)
test_r2 = r2_score(testY_plot, test_predict)

# Plot the training and testing loss
plt.figure(figsize=(10, 5))
plt.plot(train_losses, label='Train Loss')
plt.plot(test_losses, label='Test Loss')
plt.xlabel('Epoch')
plt.ylabel('Loss')
plt.legend()
plt.title('Training and Testing Loss Over Epochs')
# Add MSE and R² values as annotations
plt.text(0.5, 0.9, f'MSE: {train_mse:.5f}', ha='center', va='center', transform=plt.gca().transAxes, bbox=dict(facecolor='white', alpha=0.5))
plt.text(0.5, 0.8, f'R²: {train_r2:.5f}', ha='center', va='center', transform=plt.gca().transAxes, bbox=dict(facecolor='white', alpha=0.5))
plt.tight_layout()
#plt.savefig("../fig/wind_train_test_losses.png")
plt.show()

# Testing the model performance
lstm.eval()
test_predict = lstm(X_test)

data_predict = test_predict.data.numpy().reshape(-1, 1)
dataY_plot = y_test.data.numpy().reshape(-1, 1)

# Inverse transform the predictions and actual values
data_predict = scaler_y.inverse_transform(data_predict)
dataY_plot = scaler_y.inverse_transform(dataY_plot)

# Compute MSE and R2
mse = mean_squared_error(dataY_plot, data_predict)
r2 = r2_score(dataY_plot, data_predict)

# Get the test datestamps
test_dates = df.index[-len(data_predict):]  # Adjusted to match the length of data_predict

# Plot observed and predicted values
plt.figure(figsize=(12, 6))
plt.axvline(x=test_dates[0], c='r', linestyle='--', label='Train/Test Split')
plt.plot(test_dates, dataY_plot, label='Observed')
plt.plot(test_dates, data_predict, label='Predicted')
plt.suptitle(r'$Temperature Prediction$')
plt.xlabel('Year')
plt.ylabel(r'$wv(m/s)$')
plt.legend()
# Add MSE and R² values as annotations
plt.text(0.5, 0.9, f'MSE: {test_mse:.5f}', ha='center', va='center', transform=plt.gca().transAxes, bbox=dict(facecolor='white', alpha=0.5))
plt.text(0.5, 0.8, f'R²: {test_r2:.5f}', ha='center', va='center', transform=plt.gca().transAxes, bbox=dict(facecolor='white', alpha=0.5))
plt.tight_layout()
#plt.savefig("../fig/X_test_temperature.png")
plt.show()

def plot_time_series(data, X_train, X_test, y_train, y_test, y_pred_train, y_pred_test, model_name, train_r2, test_r2, scaler):
    # Inverse normalize the predicted values
    y_pred_train_inv = scaler.inverse_transform(y_pred_train.reshape(-1, 1))
    y_pred_test_inv = scaler.inverse_transform(y_pred_test.reshape(-1, 1))

    # Plot the actual values for the training dataset
    plt.figure(figsize=(12, 6))
    plt.plot(data.index[:len(X_train)], data['T(degC)'][:len(X_train)], label='Training Data(Actual)')

    # Create a common index for the test data
    test_index = data.index[len(X_train):len(X_train) + len(y_test)]

    # Plot the actual values for the test dataset in blue
    plt.plot(test_index, scaler.inverse_transform(y_test.reshape(-1, 1)), label='Test Data(Actual)', color='blue', alpha=0.5)

    # Plot the predicted values for the test dataset in green
    plt.plot(test_index, y_pred_test_inv, label='Test Data(Predicted)', alpha=0.6, color='red',)

    # Plot the predicted values for the training dataset in orange
    #plt.plot(data.index[:len(X_train)], y_pred_train_inv, label='Training Data(Predicted)', color='blue', alpha=0.6)

    # Add a vertical line at the beginning of the test data
    plt.axvline(x=test_index[0], color='red', linestyle='--', label='70%,30% data split')

    # Add R2 scores to the plot
    plt.text(0.3, 0.85, f'Train R2 Score: {train_r2:.4f}', transform=plt.gca().transAxes, fontsize=12)
    plt.text(0.3, 0.8, f'Test R2 Score: {test_r2:.4f}', transform=plt.gca().transAxes, fontsize=12)

    plt.xlabel('Date')
    plt.ylabel(r'$T(degC)$')
    plt.grid(True)
    plt.legend(loc='best')
    # Save the plot to a file with the model name
    save_path = f'{str(model_name)}_temperature_plot.png'
    plt.savefig(save_path, format='png', dpi=300)
    plt.show()

# After training the model
# Generate predictions for both train and test datasets
train_predict = lstm(X_train).data.numpy()
test_predict = lstm(X_test).data.numpy()

# Plot both train and test datasets
plot_time_series(df, X_train, X_test, y_train, y_test, train_predict, test_predict, 'LSTM', train_r2, test_r2, scaler_y)

