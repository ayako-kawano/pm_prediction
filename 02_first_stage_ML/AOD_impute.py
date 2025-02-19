#!/usr/bin/env python3
"""
Run AOD Model Update Pipeline with XGBoost

This script performs the following steps:
  1. Loads the preprocessed feature file (df_for_imputation.csv) from a given directory.
  2. Optionally merges grid (50km) information, adds a year‐month column, and randomly samples the data by grid (50km) and year‐month.
  3. Trains an AOD model using XGBoost with outer cross‐validation.
  4. Applies the final model to impute missing AOD values.

All file paths are built relative to the global variable path_to_data.
"""

import os
import sys
import math
import random
import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold, RandomizedSearchCV, shuffle
from sklearn.metrics import r2_score, mean_squared_error
from xgboost import XGBRegressor

# ------------------------------
# Global Path Variable
# ------------------------------
path_to_data = "path_to_data"  # Set your base data directory here

# ------------------------------
# Section 1: Load and Sample Preprocessed Features
# ------------------------------
def load_and_sample_df():
    """
    Load the preprocessed features file and sample it.
    """
    input_file = os.path.join(path_to_data, "ML_full_model", "df_for_imputation.csv")
    df = pd.read_csv(input_file)
    df['date'] = pd.to_datetime(df['date'])
    print("Loaded df_for_imputation.csv, shape:", df.shape)
    
    # Optionally merge with 50km grid data
    grid_file = os.path.join(path_to_data, "ML_full_model", "grid_intersect_with_50km.csv")
    if os.path.exists(grid_file):
        grid_50km = pd.read_csv(grid_file)
        grid_50km = grid_50km.rename(columns={'grid_id_10km': 'grid_id'})
        grid_50km['grid_id'] = grid_50km['grid_id'].astype(int).astype(str)
        df['grid_id'] = df['grid_id'].astype(int).astype(str)
        df = pd.merge(df, grid_50km, how='left', on='grid_id')
        print("After merging with grid_50km, shape:", df.shape)
    
    # Create a year-month column
    df['year_month'] = df['date'].dt.strftime('%Y-%m')
    
    # Randomly sample 3% of records per group (by grid_id_50km and year_month)
    df_sampled = df.groupby(['grid_id_50km', 'year_month']).apply(
        lambda x: x.sample(frac=0.03, random_state=42, replace=False)
    ).reset_index(drop=True)
    
    print("Sampled df shape:", df_sampled.shape)
    print("Unique grid_id in sample:", df_sampled['grid_id'].nunique())
    print("Unique grid_id_50km in sample:", df_sampled['grid_id_50km'].nunique())
    
    # Save the sampled DataFrame for reference
    output_sampled = os.path.join(path_to_data, "ML_full_model", "aod_ml_df_sampled.csv")
    df_sampled.to_csv(output_sampled, index=False)
    print("Sampled data saved to:", output_sampled)
    return df_sampled

# ------------------------------
# Section 2: Outer CV Training with XGBoost
# ------------------------------
def run_outer_cv(df, features, target, group_col, best_params_XGB, output_dir):
    """
    Runs outer cross-validation training using XGBoost.
    """
    X = df[features].copy()
    y = df[target].copy()
    gkf_outer = GroupKFold(n_splits=10)
    outer_cv = list(gkf_outer.split(X, y, groups=df[group_col]))
    
    trn_r2, trn_rmse, cv_r2, cv_rmse = [], [], [], []
    dfs_importance, train_dfs, eval_dfs = [], [], []
    drop_cols = ['date', 'grid_id', 'grid_id_50km', 'year_month']
    
    for fold, (trn_idx, val_idx) in enumerate(outer_cv):
        print(f"\n===== Outer Fold {fold+1} =====")
        X_trn, X_val = X.iloc[trn_idx], X.iloc[val_idx]
        y_trn, y_val = y.iloc[trn_idx], y.iloc[val_idx]
    
        # Save identifying columns for record-keeping
        train_df = X_trn[['date', 'grid_id']].copy()
        train_df['y_trn'] = y_trn
        eval_df = X_val[['date', 'grid_id']].copy()
        eval_df['y_val'] = y_val
        
        # Drop non-modeling columns
        X_trn_model = X_trn.drop(columns=drop_cols, errors='ignore')
        X_val_model = X_val.drop(columns=drop_cols, errors='ignore')
        
        # Initialize and fit XGBoost
        model_xgb = XGBRegressor(**best_params_XGB, n_jobs=int(os.getenv("SLURM_CPUS_PER_TASK", 4)), tree_method='gpu_hist')
        model_xgb.fit(X_trn_model, y_trn.values.ravel())
    
        # Feature importance
        imp_df = pd.DataFrame({
            'feature': X_trn_model.columns,
            'importance': model_xgb.feature_importances_
        }).sort_values(by='importance', ascending=False)
        dfs_importance.append(imp_df)
    
        # Training metrics
        y_trn_pred = model_xgb.predict(X_trn_model)
        r2_trn = r2_score(y_trn, y_trn_pred)
        rmse_trn = math.sqrt(mean_squared_error(y_trn, y_trn_pred))
        trn_r2.append(r2_trn)
        trn_rmse.append(rmse_trn)
        train_df['trn_y_pred'] = y_trn_pred
        train_dfs.append(train_df)
    
        # Validation metrics
        y_val_pred = model_xgb.predict(X_val_model)
        eval_df['y_pred'] = y_val_pred
        eval_dfs.append(eval_df)
        r2_val = r2_score(y_val, y_val_pred)
        rmse_val = math.sqrt(mean_squared_error(y_val, y_val_pred))
        cv_r2.append(r2_val)
        cv_rmse.append(rmse_val)
    
        print(f"Fold {fold+1}: Train R2: {r2_trn:.3f}, Train RMSE: {rmse_trn:.3f} | CV R2: {r2_val:.3f}, CV RMSE: {rmse_val:.3f}")
    
    print("\nOverall Performance:")
    print(f"Average Train R2: {np.mean(trn_r2):.3f}, Average Train RMSE: {np.mean(trn_rmse):.3f}")
    print(f"Average CV R2: {np.mean(cv_r2):.3f}, Average CV RMSE: {np.mean(cv_rmse):.3f}")
    
    # Save per-fold results
    for i in range(len(outer_cv)):
        dfs_importance[i].to_csv(os.path.join(output_dir, f"fold_{i+1}_XGB_feature_importance.csv"), index=False)
        train_dfs[i].to_csv(os.path.join(output_dir, f"fold_{i+1}_XGB_traindf.csv"), index=False)
        eval_dfs[i].to_csv(os.path.join(output_dir, f"fold_{i+1}_XGB_evaldf.csv"), index=False)
    
    return model_xgb

# ------------------------------
# Section 3: Final Pipeline & Imputation
# ------------------------------
def main():
    # Step 1: Load and sample the preprocessed features.
    df_sampled = load_and_sample_df()
    
    # Step 2: Load the sampled data for ML training.
    ml_input_file = os.path.join(path_to_data, "ML_full_model", "aod_ml_df_sampled.csv")
    df_ml = pd.read_csv(ml_input_file)
    df_ml['date'] = pd.to_datetime(df_ml['date'])
    df_ml['grid_id'] = df_ml['grid_id'].astype(str)
    
    # Define features and target for AOD.
    feature_cols = ['aot_daily', 'co_daily', 'v_wind', 'u_wind', 'rainfall', 'temp',
                    'pressure', 'thermal_radiation', 'low_veg', 'high_veg', 'dewpoint_temp',
                    'month', 'day_of_year', 'cos_day_of_year', 'monsoon', 'lon', 'lat',
                    'wind_degree', 'RH', 'aot_rolling', 'co_rolling', 'omi_no2_rolling',
                    'v_wind_rolling', 'u_wind_rolling', 'rainfall_rolling', 'temp_rolling',
                    'wind_degree_rolling', 'RH_rolling', 'thermal_radiation_rolling',
                    'dewpoint_temp_rolling', 'aot_daily_annual', 'co_daily_annual',
                    'omi_no2_annual', 'v_wind_annual', 'u_wind_annual', 'rainfall_annual',
                    'thermal_radiation_annual', 'low_veg_annual', 'high_veg_annual',
                    'dewpoint_temp_annual', 'wind_degree_annual', 'RH_annual', 'co_daily_allyears']
    target_col = 'aod'
    
    # Step 3: Run outer CV training with XGBoost.
    OUTPUT_ML_DIR = os.path.join(path_to_data, "ML_full_model", "AOD_impute")
    
    # Fixed XGBoost parameters (set based on previous tuning)
    best_params_XGB = {
        'subsample': 0.8,
        'n_estimators': 1000,
        'min_child_weight': 1,
        'max_depth': 20,
        'lambda': 100,
        'gamma': 0.8,
        'eta': 0.1,
        'booster': 'gbtree'
    }
    
    final_model = run_outer_cv(df_ml, feature_cols, target_col, 'grid_id_50km',
                               best_params_XGB, output_dir=OUTPUT_ML_DIR)
    
    # Step 4: Final Imputation on Missing AOD Data.
    missing_file = os.path.join(path_to_data, "ML_full_model", "aod_missing_to_be_imputed.csv")
    df_missing = pd.read_csv(missing_file)
    df_missing['date'] = pd.to_datetime(df_missing['date'])
    X_fin = df_missing[feature_cols].copy()
    pred = final_model.predict(X_fin)
    df_missing['AOD_imputed'] = pred
    imputed_out = os.path.join(OUTPUT_ML_DIR, "aod_imputed_XGB.csv")
    df_missing.to_csv(imputed_out, index=False)
    print("Final imputed AOD saved to:", imputed_out)

if __name__ == '__main__':
    main()
