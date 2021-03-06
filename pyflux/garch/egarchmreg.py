import sys
if sys.version_info < (3,):
    range = xrange

import numpy as np
import pandas as pd
import scipy.stats as ss
import matplotlib.pyplot as plt
import seaborn as sns
from patsy import dmatrices, dmatrix, demo_data

from .. import inference as ifr
from .. import distributions as dst
from .. import output as op
from .. import tests as tst
from .. import tsm as tsm
from .. import gas as gas
from .. import data_check as dc

class EGARCHMReg(tsm.TSM):
    """ Inherits time series methods from TSM class.

    **** BETA-t-EGARCH IN MEAN REGRESSION MODELS ****

    Parameters
    ----------
    data : pd.DataFrame
        Field to specify the time series data that will be used.

    p : int
        Field to specify how many GARCH terms the model will have.

    q : int
        Field to specify how many SCORE terms the model will have.

    formula : string
        patsy string describing the regression

    """

    def __init__(self, data, p, q, formula):

        # Initialize TSM object
        super(EGARCHMReg,self).__init__('EGARCHMReg')

        # Latent variables
        self.p = p
        self.q = q
        self.max_lag = max(self.p,self.q)  
        self.z_no = self.p + self.q + 2
        self._z_hide = 0 # Whether to cutoff variance latent variables from results
        self.supported_methods = ["MLE","PML","Laplace","M-H","BBVI"]
        self.default_method = "MLE"
        self.multivariate_model = False
        self.leverage = False
        self.model_name = "EGARCHMReg(" + str(self.p) + "," + str(self.q) + ")"

        # Format the data
        self.is_pandas = True # This is compulsory for this model type
        self.data_original = data
        self.formula = formula
        self.y, self.X = dmatrices(formula, data)
        self.z_no += self.X.shape[1]*2
        self.y_name = self.y.design_info.describe()
        self.data_name = self.y_name
        self.X_names = self.X.design_info.describe().split(" + ")
        self.y = np.array([self.y]).ravel()
        self.data = self.y
        self.X = np.array([self.X])[0]
        self.index = data.index
        self.initial_values = np.zeros(self.z_no)

        self._create_latent_variables()

    def _create_latent_variables(self):
        """ Creates model latent variables

        Returns
        ----------
        None (changes model attributes)
        """

        for p_term in range(self.p):
            self.latent_variables.add_z('p(' + str(p_term+1) + ')',ifr.Normal(0,0.5,transform='logit'),dst.q_Normal(0,3))
            if p_term == 0:
                self.latent_variables.z_list[-1].start = 3.00
            else:
                self.latent_variables.z_list[-1].start = -4.00

        for q_term in range(self.q):
            self.latent_variables.add_z('q(' + str(q_term+1) + ')',ifr.Normal(0,0.5,transform='logit'),dst.q_Normal(0,3))
            if q_term == 0:
                self.latent_variables.z_list[-1].start = -1.50  
            else: 
                self.latent_variables.z_list[-1].start = -4.00  

        self.latent_variables.add_z('v', ifr.Uniform(transform='exp'), dst.q_Normal(0,3))
        self.latent_variables.add_z('GARCH-M', ifr.Normal(0, 3, transform=None),dst.q_Normal(0,3))

        for parm in range(len(self.X_names)):
            self.latent_variables.add_z('Vol Beta ' + self.X_names[parm],ifr.Normal(0,10,transform=None),dst.q_Normal(0,3))

        for parm in range(len(self.X_names)):
            self.latent_variables.add_z('Returns Beta ' + self.X_names[parm],ifr.Normal(0,10,transform=None),dst.q_Normal(0,3))

        # Starting values        

        for i in range(self.p+self.q, self.z_no):
            self.latent_variables.z_list[i].start = 0.0

        self.latent_variables.z_list[self.p+self.q].start = 2.0

    def _model(self, beta):
        """ Creates the structure of the model

        Parameters
        ----------
        beta : np.array
            Contains untransformed starting values for latent variables

        Returns
        ----------
        lambda : np.array
            Contains the values for the conditional volatility series

        Y : np.array
            Contains the length-adjusted time series (accounting for lags)

        scores : np.array
            Contains the score terms for the time series
        """

        Y = np.array(self.data[self.max_lag:self.data.shape[0]])
        X = np.ones(Y.shape[0])
        scores = np.zeros(Y.shape[0])

        # Transform latent variables
        parm = np.array([self.latent_variables.z_list[k].prior.transform(beta[k]) for k in range(beta.shape[0])])

        lmda = np.zeros(Y.shape[0])
        theta = np.zeros(Y.shape[0])

        # Loop over time series
        for t in range(0,Y.shape[0]):
            if t < self.max_lag:
                lmda[t] = parm[-len(self.X_names)*2]/(1-np.sum(parm[:self.p]))
                theta[t] = np.dot(self.X[t],parm[-len(self.X_names):])
            else:
                # Loop over GARCH terms
                for p_term in range(0,self.p):
                    lmda[t] += parm[p_term]*lmda[t-p_term-1]

                # Loop over Score terms
                for q_term in range(0,self.q):
                    lmda[t] += parm[self.p+q_term]*scores[t-q_term-1]

                if self.leverage is True:
                    lmda[t] += parm[-(len(self.X_names)*2)-3]*np.sign(-(Y[t-1]-theta[t-1]))*(scores[t-1]+1)

                lmda[t] += np.dot(self.X[t],parm[-len(self.X_names)*2:-len(self.X_names)])

                theta[t] = np.dot(self.X[t],parm[-len(self.X_names):]) + parm[-(len(self.X_names)*2)-1]*np.exp(lmda[t]/2.0)
            
            scores[t] = (((parm[self.p+self.q]+1.0)*np.power(Y[t]-theta[t],2))/float(parm[self.p+self.q]*np.exp(lmda[t]) + np.power(Y[t]-theta[t],2))) - 1.0        

        return lmda, Y, scores, theta

    def _mean_prediction(self, lmda, Y, scores, h, t_params, X_oos):
        """ Creates a h-step ahead mean prediction

        Parameters
        ----------
        lmda : np.array
            The past predicted values

        Y : np.array
            The past data

        scores : np.array
            The past scores

        h : int
            How many steps ahead for the prediction

        t_params : np.array
            A vector of (transformed) latent variables

        X_oos : np.array
            Out of sample predictors

        Returns
        ----------
        h-length vector of mean predictions
        """     

        # Create arrays to iteratre over
        lmda_exp = lmda.copy()
        scores_exp = scores.copy()
        Y_exp = Y.copy()

        # Loop over h time periods          
        for t in range(0,h):
            new_lambda_value = 0

            if self.p != 0:
                for j in range(self.p):
                    new_lambda_value += t_params[j]*lmda_exp[-j-1]

            if self.q != 0:
                for k in range(self.q):
                    new_lambda_value += t_params[k+self.p]*scores_exp[-k-1]

            # No leverage term for mean (should be zero in expectation?)

            new_lambda_value += np.dot(X_oos[t],t_params[-len(self.X_names)*2:-len(self.X_names)]) 
            new_theta_value = np.dot(X_oos[t],t_params[-len(self.X_names):]) + t_params[-(len(self.X_names)*2)-1]*np.exp(new_lambda_value/2.0)
            lmda_exp = np.append(lmda_exp,[new_lambda_value]) # For indexing consistency
            scores_exp = np.append(scores_exp,[0]) # expectation of score is zero
            Y_exp = np.append(Y_exp,new_theta_value)

        return lmda_exp

    def _sim_prediction(self, lmda, Y, scores, h, t_params, simulations, X_oos):
        """ Simulates a h-step ahead mean prediction

        Parameters
        ----------
        lmda : np.array
            The past predicted values

        Y : np.array
            The past data

        scores : np.array
            The past scores

        h : int
            How many steps ahead for the prediction

        t_params : np.array
            A vector of (transformed) latent variables

        simulations : int
            How many simulations to perform

        X_oos : np.array
            Out of sample predictors

        Returns
        ----------
        Matrix of simulations
        """     

        sim_vector = np.zeros([simulations,h])

        for n in range(0,simulations):
            # Create arrays to iteratre over
            lmda_exp = lmda.copy()
            scores_exp = scores.copy()
            Y_exp = Y.copy()

            # Loop over h time periods          
            for t in range(0,h):
                new_lambda_value = 0

                if self.p != 0:
                    for j in range(self.p):
                        new_lambda_value += t_params[j]*lmda_exp[-j-1]

                if self.q != 0:
                    for k in range(self.q):
                        new_lambda_value += t_params[k+self.p]*scores_exp[-k-1]

                if self.leverage is True:
                    new_lambda_value += t_params[self.p+self.q]*np.sign(-(Y_exp[-1]-new_theta_value))*(scores_exp[-1]+1)

                new_lambda_value += np.dot(X_oos[t],t_params[-len(self.X_names)*2:-len(self.X_names)]) 
                new_theta_value = np.dot(X_oos[t],t_params[-len(self.X_names):]) + t_params[-(len(self.X_names)*2)-1]*np.exp(new_lambda_value/2.0)

                lmda_exp = np.append(lmda_exp,[new_lambda_value]) # For indexing consistency
                scores_exp = np.append(scores_exp,scores[np.random.randint(scores.shape[0])]) # expectation of score is zero
                Y_exp = np.append(Y_exp,Y[np.random.randint(Y.shape[0])]) # bootstrap returns

            sim_vector[n] = lmda_exp[-h:]

        return np.transpose(sim_vector)

    def _summarize_simulations(self, mean_values, sim_vector, date_index, h, past_values):
        """ Summarizes a simulation vector and a mean vector of predictions

        Parameters
        ----------
        mean_values : np.array
            Mean predictions for h-step ahead forecasts

        sim_vector : np.array
            N simulation predictions for h-step ahead forecasts

        date_index : pd.DateIndex or np.array
            Dates for the simulations

        h : int
            How many steps ahead are forecast

        past_values : int
            How many past observations to include in the forecast plot

        intervals : Boolean
            Would you like to show prediction intervals for the forecast?
        """ 

        error_bars = []
        for pre in range(5,100,5):
            error_bars.append(np.insert([np.percentile(i,pre) for i in sim_vector] - mean_values[-h:],0,0))
        forecasted_values = mean_values[-h-1:]
        plot_values = mean_values[-h-past_values:]
        plot_index = date_index[-h-past_values:]
        return error_bars, forecasted_values, plot_values, plot_index

    def add_leverage(self):
        """ Adds leverage term to the model

        Returns
        ----------
        None (changes instance attributes)
        """             

        if self.leverage is True:
            pass
        else:
            self.leverage = True
            self.z_no += 1
            self.latent_variables.z_list.pop()            
            self.latent_variables.z_list.pop()
            self.latent_variables.z_list.pop()
            self.latent_variables.add_z('Leverage Term',ifr.Uniform(transform=None),dst.q_Normal(0,3))
            self.latent_variables.add_z('v',ifr.Uniform(transform='exp'),dst.q_Normal(0,3))
            self.latent_variables.add_z('Returns Constant',ifr.Normal(0,3,transform=None),dst.q_Normal(0,3))
            self.latent_variables.add_z('GARCH-M',ifr.Normal(0,3,transform=None),dst.q_Normal(0,3))
            self.latent_variables.z_list[-3].start = 2.0

    def neg_loglik(self, beta):
        """ Creates the negative log-likelihood of the model

        Parameters
        ----------
        beta : np.array
            Contains untransformed starting values for latent variables

        Returns
        ----------
        The negative logliklihood of the model
        """     

        lmda, Y, _, theta = self._model(beta)
        return -np.sum(ss.t.logpdf(x=Y,
            df=self.latent_variables.z_list[-(len(self.X_names)*2)-2].prior.transform(beta[-(len(self.X_names)*2)-2]),
            loc=theta,scale=np.exp(lmda/2.0)))
    
    def plot_fit(self, **kwargs):
        """ Plots the fit of the model

        Returns
        ----------
        None (plots data and the fit)
        """

        figsize = kwargs.get('figsize',(10,7))

        if self.latent_variables.estimated is False:
            raise Exception("No latent variables estimated!")
        else:
            t_params = self.transform_z()
            plt.figure(figsize=figsize)
            date_index = self.index[max(self.p,self.q):]
            sigma2, Y, ___, theta = self._model(self.latent_variables.get_z_values())
            plt.plot(date_index,np.abs(Y-theta),label=self.data_name + ' Absolute Demeaned Values')
            plt.plot(date_index,np.exp(sigma2/2.0),label='EGARCHMREG(' + str(self.p) + ',' + str(self.q) + ') Conditional Volatility',c='black')                   
            plt.title(self.data_name + " Volatility Plot")  
            plt.legend(loc=2)   
            plt.show()              

    def plot_predict(self, h=5, past_values=20, intervals=True, oos_data=None, **kwargs):

        """ Plots forecast with the estimated model

        Parameters
        ----------
        h : int (default : 5)
            How many steps ahead would you like to forecast?

        past_values : int (default : 20)
            How many past observations to show on the forecast graph?

        intervals : Boolean
            Would you like to show prediction intervals for the forecast?

        oos_data : pd.DataFrame
            Data for the variables to be used out of sample (ys can be NaNs)

        Returns
        ----------
        - Plot of the forecast
        """     

        figsize = kwargs.get('figsize',(10,7))

        if self.latent_variables.estimated is False:
            raise Exception("No latent variables estimated!")
        else:

            # Retrieve data, dates and (transformed) latent variables
            _, X_oos = dmatrices(self.formula, oos_data)
            X_oos = np.array([X_oos])[0]
            X_pred = X_oos[:h]
            lmda, Y, scores, theta = self._model(self.latent_variables.get_z_values())           
            date_index = self.shift_dates(h)
            t_params = self.transform_z()

            # Get mean prediction and simulations (for errors)
            mean_values = self._mean_prediction(lmda,Y,scores,h,t_params,X_pred)
            sim_values = self._sim_prediction(lmda,Y,scores,h,t_params,15000,X_pred)
            error_bars, forecasted_values, plot_values, plot_index = self._summarize_simulations(mean_values,sim_values,date_index,h,past_values)

            plt.figure(figsize=figsize)
            if intervals == True:
                alpha =[0.15*i/float(100) for i in range(50,12,-2)]
                for count, pre in enumerate(error_bars):
                    plt.fill_between(date_index[-h-1:], np.exp((forecasted_values-pre)/2), np.exp((forecasted_values+pre)/2),alpha=alpha[count])            
            
            plt.plot(plot_index,np.exp(plot_values/2.0))
            plt.title("Forecast for " + self.data_name + " Conditional Volatility")
            plt.xlabel("Time")
            plt.ylabel(self.data_name)
            plt.show()

    def predict_is(self, h=5, fit_once=True):
        """ Makes dynamic in-sample predictions with the estimated model

        Parameters
        ----------
        h : int (default : 5)
            How many steps would you like to forecast?

        fit_once : boolean
            (default: True) Fits only once before the in-sample prediction; if False, fits after every new datapoint

        Returns
        ----------
        - pd.DataFrame with predicted values
        """  

        predictions = []

        for t in range(0, h):
            data1 = self.data_original.iloc[:-(h+t),:]
            data2 = self.data_original.iloc[-h+t:,:]
            x = EGARCHMReg(p=self.p, q=self.q, data=self.data_original[:(-h+t)],formula=self.formula)

            if fit_once is False:
                x.fit(printer=False)

            if t == 0:
                if fit_once is True:
                    x.fit(printer=False)
                    saved_lvs = x.latent_variables
                predictions = x.predict(h=1, oos_data=data2)
            else:
                if fit_once is True:
                    x.latent_variables = saved_lvs
                predictions = pd.concat([predictions,x.predict(h=1, oos_data=data2)])
        
        predictions.rename(columns={0:self.y_name}, inplace=True)
        predictions.index = self.index[-h:]

        return predictions

    def plot_predict_is(self, h=5, fit_once=True, **kwargs):
        """ Plots forecasts with the estimated model against data
            (Simulated prediction with data)

        Parameters
        ----------
        h : int (default : 5)
            How many steps to forecast

        fit_once : boolean
            (default: True) Fits only once before the in-sample prediction; if False, fits after every new datapoint

        Returns
        ----------
        - Plot of the forecast against data 
        """     

        figsize = kwargs.get('figsize',(10,7))

        plt.figure(figsize=figsize)
        date_index = self.index[-h:]
        predictions = self.predict_is(h=h, fit_once=fit_once)
        data = self.data[-h:]

        t_params = self.transform_z()

        plt.plot(date_index,np.abs(data-t_params[-1]),label='Data')
        plt.plot(date_index,predictions,label='Predictions',c='black')
        plt.title(self.data_name)
        plt.legend(loc=2)   
        plt.show()          

    def predict(self, h=5, oos_data=None):
        """ Makes forecast with the estimated model

        Parameters
        ----------
        h : int (default : 5)
            How many steps ahead would you like to forecast?

        oos_data : pd.DataFrame
            Data to use for the predictors in the forecast

        Returns
        ----------
        - pd.DataFrame with predicted values
        """     

        if self.latent_variables.estimated is False:
            raise Exception("No latent variables estimated!")
        else:
            _, X_oos = dmatrices(self.formula, oos_data)
            X_oos = np.array([X_oos])[0]
            X_pred = X_oos[:h]

            sigma2, Y, scores, _ = self._model(self.latent_variables.get_z_values()) 
            date_index = self.shift_dates(h)
            t_params = self.transform_z()

            mean_values = self._mean_prediction(sigma2,Y,scores,h,t_params,X_pred)
            forecasted_values = mean_values[-h:]
            result = pd.DataFrame(np.exp(forecasted_values/2.0))
            result.rename(columns={0:self.data_name}, inplace=True)
            result.index = date_index[-h:]

            return result


