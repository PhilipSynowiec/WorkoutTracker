#!/usr/bin/env python3
"""
Real-time webcam exercise detection using asynchronous processing with segmentation
"""

import cv2
import numpy as np
import tensorflow as tf
import time
import os
import sys
import threading
from collections import deque
from typing import Optional, Dict, List, Tuple
from queue import Queue
import csv

# Add the src directory to the path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from core.dataset_builder import FeatureExtractor
from core.realtime_pipeline import ExerciseClassifier


class SegmentationProcessor:
    """Real-time segmentation processor for exercise repetition detection."""
    
    def __init__(self, models_dir: str = "models/segmentation"):
        """
        Initialize segmentation processor with exercise-specific models.
        
        Args:
            models_dir: Directory containing segmentation models
        """
        self.models_dir = models_dir
        self.models = {}
        self.exercise_types = ['push-ups', 'squats', 'pull-ups', 'dips']
        
        # Load all segmentation models
        for exercise in self.exercise_types:
            model_path = os.path.join(models_dir, f"{exercise}.keras")
            if os.path.exists(model_path):
                self.models[exercise] = tf.keras.models.load_model(model_path, compile=False)
                print(f"Loaded {exercise} segmentation model")
            else:
                print(f"Warning: {exercise} segmentation model not found at {model_path}")
        
        # CSV logging for probabilities
        self.csv_file = open("segmentation_probabilities.csv", "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["frame", "timestamp", "exercise", "probability"])
        self.frame_counter = 0
    
    def predict_window(self, window: np.ndarray, exercise_type: str) -> float:
        """
        Predict segmentation probability for a 30-frame window.
        
        Args:
            window: 30-frame sequence of joint angles (30, 25)
            exercise_type: Type of exercise to predict for
            
        Returns:
            Probability score (0-1) indicating if window contains a rep
        """
        if exercise_type not in self.models:
            return 0.0
        
        # Model expects (1, window_size, num_features)
        sequence = window[None, ...]
        
        # Get prediction - model outputs (1, window_size, 1)
        # We take the last frame probability as the current rep probability
        probability = self.models[exercise_type].predict(sequence, verbose=0)
        prob_value = float(probability[0, -1, 0])  # Last frame probability
        
        # Log to CSV
        self.frame_counter += 1
        self.csv_writer.writerow([self.frame_counter, time.time(), exercise_type, prob_value])
        
        # Flush every 10 predictions for performance
        if self.frame_counter % 10 == 0:
            self.csv_file.flush()
        
        return prob_value
    
    def close(self):
        """Close CSV file."""
        self.csv_file.close()


class WebcamRealtimePipeline:
    """Real-time webcam exercise detection with asynchronous processing and segmentation."""
    
    def __init__(self, 
                 classifier_model: str = "models/classification/exercise_classifier.keras",
                 segmentation_models_dir: str = "models/segmentation",
                 window_size: int = 30,
                 webcam_id: int = 0):
        """
        Initialize the webcam real-time pipeline.
        
        Args:
            classifier_model: Path to classification model
            segmentation_models_dir: Directory with segmentation models
            window_size: Window size for classification and segmentation
            webcam_id: Webcam device ID (usually 0 for default webcam)
        """
        self.classifier = ExerciseClassifier(classifier_model, window_size)
        self.segmentation = SegmentationProcessor(segmentation_models_dir)
        self.feature_extractor = FeatureExtractor()
        self.window_size = window_size
        self.webcam_id = webcam_id
        
        # Shared state between threads
        self.current_window = np.zeros((window_size, 25), dtype=np.float32)
        self.window_filled = False
        self.current_exercise = "unknown"
        self.exercise_confidence = 0.0
        self.current_probability = 0.0
        self.frame_count = 0
        self.processed_frame_count = 0
        
        # Threading
        self.feature_queue = Queue(maxsize=100)  # Buffer for features
        self.result_queue = Queue(maxsize=10)    # Buffer for results
        self.running = True
        
        # Performance tracking
        self.processing_times = deque(maxlen=30)
        self.fps_times = deque(maxlen=30)
        
        # Results tracking
        self.exercise_predictions = []
        self.confidence_scores = []
        self.probability_scores = []
        
        print(f"Webcam real-time pipeline initialized with segmentation")
        print(f"Press 'q' to quit, 'r' to reset counts")
    
    def run(self):
        """Run the real-time webcam pipeline."""
        # Initialize webcam
        cap = cv2.VideoCapture(self.webcam_id)
        
        if not cap.isOpened():
            print(f"Error: Could not open webcam (ID: {self.webcam_id})")
            return
        
        # Set webcam properties for better performance
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)
        
        # Get actual webcam properties
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        print(f"Webcam properties: {width}x{height}, {fps} FPS")
        
        # Start background processing thread
        processing_thread = threading.Thread(target=self._background_processor)
        processing_thread.start()
        
        print("Starting real-time webcam exercise detection with segmentation...")
        print("Perform exercises in front of the camera!")
        
        last_fps_time = time.time()
        
        try:
            while True:
                frame_start_time = time.time()
                
                # Capture frame
                ret, frame = cap.read()
                if not ret:
                    print("Failed to grab frame from webcam")
                    break
                
                # Extract features and add to queue (non-blocking)
                features = self.feature_extractor.extract_angles(frame)
                if features is not None:
                    try:
                        self.feature_queue.put_nowait((self.frame_count, features))
                    except:
                        pass  # Queue full, skip this frame
                
                # Update current window (shift and add new frame)
                if features is not None:
                    self.current_window = np.roll(self.current_window, -1, axis=0)
                    self.current_window[-1] = features
                    
                    # Check if window is filled
                    if not self.window_filled and self.frame_count >= self.window_size - 1:
                        self.window_filled = True
                
                # Check for new results from background thread
                try:
                    while not self.result_queue.empty():
                        exercise, confidence, probability = self.result_queue.get_nowait()
                        self.current_exercise = exercise
                        self.exercise_confidence = confidence
                        self.current_probability = probability
                except:
                    pass
                
                # Store predictions for analysis
                self.exercise_predictions.append(self.current_exercise)
                self.confidence_scores.append(self.exercise_confidence)
                self.probability_scores.append(self.current_probability)
                
                # Calculate FPS
                current_time = time.time()
                self.fps_times.append(current_time - last_fps_time)
                last_fps_time = current_time
                
                # Draw overlay
                self._draw_overlay(frame)
                
                # Display frame
                cv2.imshow('Real-Time Exercise Detection with Segmentation - Webcam', frame)
                
                # Handle key presses
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    break
                elif key == ord('r'):
                    self._reset_all_counts()
                
                self.frame_count += 1
        
        finally:
            # Stop background processing
            self.running = False
            processing_thread.join()
            
            cap.release()
            cv2.destroyAllWindows()
            self.segmentation.close()
        
        # Print final results and plot probabilities
        self._print_results()
        self._plot_probabilities()
    
    def _background_processor(self):
        """Background thread for model inference."""
        while self.running:
            try:
                # Get features from queue
                frame_idx, features = self.feature_queue.get(timeout=0.1)
                
                # Update window
                self.current_window = np.roll(self.current_window, -1, axis=0)
                self.current_window[-1] = features
                
                # Check if window is filled
                if not self.window_filled and frame_idx >= self.window_size - 1:
                    self.window_filled = True
                
                # Run models every 10th frame when window is filled
                if self.window_filled and frame_idx % 10 == 0:
                    start_time = time.time()
                    
                    # Use the current 30-frame window
                    window = self.current_window.copy()
                    
                    # Classification
                    exercise, confidence = self.classifier.predict_window(window)
                    
                    # Segmentation (only if exercise is detected)
                    probability = 0.0
                    if exercise != "unknown":
                        probability = self.segmentation.predict_window(window, exercise)
                    
                    # Add result to queue
                    try:
                        self.result_queue.put_nowait((exercise, confidence, probability))
                    except:
                        pass  # Queue full, skip result
                    
                    # Track processing time
                    processing_time = time.time() - start_time
                    self.processing_times.append(processing_time)
                    self.processed_frame_count += 1
                
            except:
                continue  # Timeout or queue empty, continue
    
    def _draw_overlay(self, frame: np.ndarray):
        """Draw information overlay on frame."""
        height, width = frame.shape[:2]
        
        # Background for text
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (width, 140), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.3, frame, 0.7, 0, frame)
        
        # Exercise information
        exercise_text = f"Exercise: {self.current_exercise.upper()}"
        confidence_text = f"Confidence: {self.exercise_confidence:.3f}"
        probability_text = f"Rep Probability: {self.current_probability:.3f}"
        
        # Color based on confidence
        if self.exercise_confidence > 0.7:
            color = (0, 255, 0)  # Green
        elif self.exercise_confidence > 0.5:
            color = (0, 255, 255)  # Yellow
        else:
            color = (0, 0, 255)  # Red
        
        cv2.putText(frame, exercise_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 
                   0.8, color, 2)
        cv2.putText(frame, confidence_text, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 
                   0.6, (255, 255, 255), 2)
        
        # Segmentation probability with color coding
        if self.current_probability > 0.7:
            prob_color = (0, 255, 0)  # Green - high probability of rep
        elif self.current_probability > 0.5:
            prob_color = (0, 255, 255)  # Yellow - medium probability
        else:
            prob_color = (0, 0, 255)  # Red - low probability
        
        cv2.putText(frame, probability_text, (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 
                   0.6, prob_color, 2)
        
        # Performance metrics
        if self.processing_times:
            avg_time = np.mean(self.processing_times) * 1000  # Convert to ms
            cv2.putText(frame, f"Model FPS: {1000/avg_time:.1f}", (width - 150, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        
        # Webcam FPS
        if self.fps_times:
            webcam_fps = 1.0 / np.mean(self.fps_times)
            cv2.putText(frame, f"Webcam FPS: {webcam_fps:.1f}", (width - 150, 55), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        
        # Instructions
        cv2.putText(frame, "Press 'q' to quit, 'r' to reset", (10, 120), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        
        # Frame counter
        cv2.putText(frame, f"Frame: {self.frame_count}", (10, 140), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    
    def _reset_all_counts(self):
        """Reset all rep counts."""
        print("Rep counts reset (not implemented in current version)")
    
    def _print_results(self):
        """Print final analysis results."""
        print("\n" + "="*50)
        print("WEBCAM REAL-TIME PROCESSING RESULTS")
        print("="*50)
        
        # Exercise classification results
        print(f"\nExercise Classification:")
        print(f"Total frames processed: {len(self.exercise_predictions)}")
        
        # Count predictions for each exercise
        from collections import Counter
        exercise_counts = Counter(self.exercise_predictions)
        for exercise, count in exercise_counts.most_common():
            percentage = count / len(self.exercise_predictions) * 100
            print(f"  {exercise}: {count} frames ({percentage:.1f}%)")
        
        # Confidence statistics
        if self.confidence_scores:
            avg_confidence = np.mean(self.confidence_scores)
            max_confidence = np.max(self.confidence_scores)
            min_confidence = np.min(self.confidence_scores)
            print(f"\nConfidence Statistics:")
            print(f"  Average: {avg_confidence:.3f}")
            print(f"  Maximum: {max_confidence:.3f}")
            print(f"  Minimum: {min_confidence:.3f}")
        
        # Segmentation probability statistics
        if self.probability_scores:
            avg_probability = np.mean(self.probability_scores)
            max_probability = np.max(self.probability_scores)
            min_probability = np.min(self.probability_scores)
            print(f"\nSegmentation Probability Statistics:")
            print(f"  Average: {avg_probability:.3f}")
            print(f"  Maximum: {max_probability:.3f}")
            print(f"  Minimum: {min_probability:.3f}")
        
        # Performance statistics
        if self.processing_times:
            avg_time = np.mean(self.processing_times) * 1000
            max_time = np.max(self.processing_times) * 1000
            min_time = np.min(self.processing_times) * 1000
            print(f"\nPerformance Statistics:")
            print(f"  Model inference time: {avg_time:.1f}ms")
            print(f"  Maximum inference time: {max_time:.1f}ms")
            print(f"  Minimum inference time: {min_time:.1f}ms")
            print(f"  Model FPS: {1000/avg_time:.1f}")
            print(f"  Frames analyzed: {self.processed_frame_count}")
    
    def _plot_probabilities(self):
        """Plot segmentation probabilities after session."""
        try:
            import pandas as pd
            import matplotlib.pyplot as plt
            
            # Read the CSV file
            df = pd.read_csv("segmentation_probabilities.csv")
            
            if len(df) == 0:
                print("No segmentation data to plot")
                return
            
            # Create the plot
            plt.figure(figsize=(15, 8))
            
            # Plot probabilities for each exercise type
            for exercise in self.segmentation.exercise_types:
                exercise_data = df[df['exercise'] == exercise]
                if len(exercise_data) > 0:
                    plt.plot(exercise_data['frame'], exercise_data['probability'], 
                            label=exercise, linewidth=2, alpha=0.8)
            
            plt.xlabel('Frame Number')
            plt.ylabel('Repetition Probability')
            plt.title('Real-Time Exercise Repetition Detection Probabilities')
            plt.legend()
            plt.grid(True, alpha=0.3)
            plt.ylim(0, 1)
            
            # Save the plot
            plt.savefig('segmentation_probabilities_plot.png', dpi=300, bbox_inches='tight')
            print(f"Segmentation probabilities plot saved to: segmentation_probabilities_plot.png")
            
            # Show the plot
            plt.show()
            
        except ImportError:
            print("Matplotlib not available, skipping probability plot")
        except Exception as e:
            print(f"Error plotting probabilities: {e}")


def main():
    """Main function for command-line usage."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Real-time webcam exercise detection with segmentation'
    )
    parser.add_argument('--classifier', type=str, 
                       default='models/classification/exercise_classifier.keras',
                       help='Path to classification model')
    parser.add_argument('--models-dir', type=str, 
                       default='models/segmentation',
                       help='Directory containing segmentation models')
    parser.add_argument('--window-size', type=int, default=30,
                       help='Window size for classification and segmentation')
    parser.add_argument('--webcam-id', type=int, default=0,
                       help='Webcam device ID (default: 0)')
    
    args = parser.parse_args()
    
    # Create and run webcam pipeline
    pipeline = WebcamRealtimePipeline(
        classifier_model=args.classifier,
        segmentation_models_dir=args.models_dir,
        window_size=args.window_size,
        webcam_id=args.webcam_id
    )
    
    pipeline.run()


if __name__ == '__main__':
    main() 